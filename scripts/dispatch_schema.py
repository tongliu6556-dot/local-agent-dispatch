import json
import pathlib
from typing import Any

SCHEMA_DIR = pathlib.Path(__file__).resolve().parents[1] / "schemas"

class SchemaValidationError(Exception):
    """Raised when a document violates structural invariants or schema requirements."""
    pass

def load_schema(name: str) -> dict[str, Any]:
    """Load a base JSON schema by name."""
    path = SCHEMA_DIR / f"{name}.schema.json"
    if not path.exists():
        raise SchemaValidationError(f"Schema {name} not found")
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)

def validate(name: str, payload: dict[str, Any]) -> None:
    """
    Validate the payload against minimum structural invariants,
    bypassing a heavy jsonschema dependency for performance and deterministic behavior.
    """
    if not isinstance(payload, dict):
        raise SchemaValidationError("Payload must be a dictionary")

    version = payload.get("schema_version")
    if version is None:
        raise SchemaValidationError("Missing schema_version")
    
    if version != 1:
        raise SchemaValidationError(f"Unsupported schema_version: {version}")

    _validate_common_fields(payload)
    if name == "task_packet":
        _validate_task_packet_contract(payload)
    elif name == "dispatch_workflow_report":
        _validate_dispatch_workflow_report_contract(payload)
    elif name == "task_capture":
        _validate_task_capture_contract(payload)
    _check_secrets(payload)

def _validate_common_fields(payload: dict[str, Any]) -> None:
    # Validate commonly structured fields if they are present
    for field in ("job", "attempt", "pool"):
        val = payload.get(field)
        if val is not None and not isinstance(val, dict):
            raise SchemaValidationError(f"malformed {field} field: must be a dict")

    # Pools usually contain mapping of pool_id -> dict
    pools = payload.get("pools")
    if pools is not None:
        if not isinstance(pools, dict):
            raise SchemaValidationError("malformed pools field: must be a dict")
        for k, v in pools.items():
            if not isinstance(v, dict):
                raise SchemaValidationError(f"malformed pool entry for {k}: must be a dict")


def _validate_task_packet_contract(payload: dict[str, Any]) -> None:
    """Validate the modern packet shape while retaining explicit migration."""
    modern = "packet_id" in payload or "attempts" in payload
    if not modern:
        return
    if payload.get("legacy_compatibility") is True:
        return
    for field in ("packet_id", "job_id", "write_scope"):
        if not isinstance(payload.get(field), str) or not payload.get(field):
            raise SchemaValidationError(f"task_packet requires non-empty {field}")
    if payload.get("validation_required") is not True:
        raise SchemaValidationError("task_packet requires validation_required=true")
    artifacts = payload.get("required_artifacts")
    if not isinstance(artifacts, list) or not artifacts or not all(isinstance(item, str) and item for item in artifacts):
        raise SchemaValidationError("task_packet requires non-empty required_artifacts")
    attempts = payload.get("attempts")
    if not isinstance(attempts, list) or not attempts:
        raise SchemaValidationError("task_packet requires non-empty attempts")
    for index, attempt in enumerate(attempts):
        if not isinstance(attempt, dict):
            raise SchemaValidationError(f"malformed task_packet attempt {index}")
        for field in ("attempt_id", "adapter", "transport", "model"):
            if not isinstance(attempt.get(field), str) or not attempt.get(field):
                raise SchemaValidationError(f"task_packet attempt {index} requires {field}")


def _validate_dispatch_workflow_report_contract(payload: dict[str, Any]) -> None:
    """Validate the stable read-only dispatch preflight envelope."""

    if payload.get("report_type") != "local-agent-dispatch.workflow":
        raise SchemaValidationError("dispatch_workflow_report requires report_type")
    for field, expected in (
        ("read_only", True),
        ("provider_execution", False),
        ("model_prompts_sent", False),
    ):
        if payload.get(field) is not expected:
            raise SchemaValidationError(
                f"dispatch_workflow_report requires {field}={str(expected).lower()}"
            )
    sequence = payload.get("sequence")
    if not isinstance(sequence, list) or len(sequence) < 5:
        raise SchemaValidationError("dispatch_workflow_report requires five ordered stages")
    stages = [row.get("stage") for row in sequence if isinstance(row, dict)]
    if stages[:5] != ["system_scan", "preflight", "task_estimate", "hardware_fit", "planner"]:
        raise SchemaValidationError("dispatch_workflow_report stage order is invalid")
    for field in ("hosts", "pools", "task_estimates", "multi_lane", "gates"):
        if not isinstance(payload.get(field), dict):
            raise SchemaValidationError(f"dispatch_workflow_report requires object field {field}")
    if not isinstance(payload.get("assignments"), list):
        raise SchemaValidationError("dispatch_workflow_report requires assignments list")


def _validate_task_capture_contract(payload: dict[str, Any]) -> None:
    """Validate the provider-free capture envelope before it feeds planning."""
    if payload.get("capture") != "bounded-task-capture":
        raise SchemaValidationError("task_capture requires capture=bounded-task-capture")
    for field, expected in (
        ("read_only", True),
        ("provider_prompts_sent", False),
        ("project_executed", False),
    ):
        if payload.get(field) is not expected:
            raise SchemaValidationError(
                f"task_capture requires {field}={str(expected).lower()}"
            )
    for field in ("task_id", "task_family"):
        if not isinstance(payload.get(field), str) or not payload[field]:
            raise SchemaValidationError(f"task_capture requires non-empty {field}")
    if not isinstance(payload.get("dag"), dict):
        raise SchemaValidationError("task_capture requires dag object")
    if not isinstance(payload.get("planner_jobs"), list):
        raise SchemaValidationError("task_capture requires planner_jobs list")
    if not isinstance(payload.get("estimate"), dict):
        raise SchemaValidationError("task_capture requires estimate object")
    if not isinstance(payload.get("unknown_semantics"), dict):
        raise SchemaValidationError("task_capture requires unknown_semantics object")

def _check_secrets(payload: Any) -> None:
    """Recursively search for keys that look like secrets/credentials."""
    # Numeric workload-cost metrics contain the word ``token`` but are not
    # credentials.  Keep the default deny rule for arbitrary token-like keys,
    # while allowing this small, schema-owned metric vocabulary.
    safe_numeric_token_keys = {
        "tokens", "input_tokens", "output_tokens", "total_tokens",
        "estimated_input_tokens", "estimated_output_tokens",
    }
    if isinstance(payload, dict):
        for k, v in payload.items():
            k_lower = str(k).lower()
            token_metric = k_lower in safe_numeric_token_keys
            if (
                any(s in k_lower for s in ("secret", "token", "password", "api_key", "credential"))
                and not token_metric
            ):
                raise SchemaValidationError(f"Secret-like field detected in public snapshot: {k}")
            _check_secrets(v)
    elif isinstance(payload, list):
        for item in payload:
            _check_secrets(item)
