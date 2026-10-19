from __future__ import annotations

from datetime import UTC, datetime

from .config import Settings
from .core import AppError, require


class PolicyGate:
    def __init__(self, settings: Settings):
        self.settings = settings

    def check(self, institution_id: str, operation: str, resource_type: str | None = None):
        institution = self.settings.institution(institution_id)
        record = next((p for p in institution.policy_records if p.operation == operation), None)
        require(record and record.state != "unknown", "POLICY_UNVERIFIED", "对应操作许可尚未核验。", "在本地档案登记有效许可依据。")
        require(record.state == "allowed", "OPERATION_NOT_PERMITTED", "档案明确禁止该操作。")
        current = datetime.now(UTC)
        require(record.verified_at and record.verified_at <= current
                and (record.expires_at is None or current < record.expires_at),
                "POLICY_UNVERIFIED", "许可已到期或尚未生效。", "重新核验许可。")
        require(record.permitted_purpose == "unrestricted"
                or self.settings.usage_purpose == record.permitted_purpose,
                "OPERATION_NOT_PERMITTED", "本许可仅适用于非商业个人科研用途。")
        if resource_type:
            require(resource_type in record.resource_types or "*" in record.resource_types,
                    "OPERATION_NOT_PERMITTED", "该文献类型不在许可范围内。")
        if operation == "external_model_use":
            require(self.settings.client.provider in record.providers,
                    "OPERATION_NOT_PERMITTED", "模型服务方不在许可范围内。")
        return record

    def can_output(self, content_class: str, rights: dict) -> bool:
        client = self.settings.client
        if content_class == "metadata":
            return True
        if client.location == "unknown" or content_class not in client.allowed_content:
            return False
        if rights.get("origin") == "import":
            recipient = "local" if client.location == "local" else client.provider
            return recipient in rights.get("allowed_recipients", [])
        institution_id = rights.get("institution_id")
        if not institution_id:
            return False
        try:
            if client.location == "external":
                self.check(institution_id, "external_model_use", rights.get("resource_type"))
            else:
                # Explicit local/index licence is also required for local content output.
                self.check(institution_id, "local_parse_index", rights.get("resource_type"))
            return True
        except AppError:
            return False

    def output(self, value):
        if isinstance(value, dict):
            if "_protected_all" in value:
                for requirement in value["_protected_all"]:
                    if not self.can_output(requirement["content_class"], requirement["rights"]):
                        return {"withheld": True, "content_class": requirement["content_class"], "reason": "OUTPUT_POLICY"}
                return self.output(value["value"])
            if "_protected" in value:
                content_class = value["_protected"]
                if self.can_output(content_class, value["rights"]):
                    return self.output(value["value"])
                return {"withheld": True, "content_class": content_class, "reason": "OUTPUT_POLICY"}
            return {key: self.output(item) for key, item in value.items() if not key.startswith("_")}
        if isinstance(value, list):
            return [self.output(item) for item in value]
        return value


def protected(value, content_class: str, rights: dict) -> dict:
    return {"_protected": content_class, "rights": rights, "value": value}


def protected_all(value, requirements: list[dict]) -> dict:
    # Flat conjunction avoids recursion depth proportional to the number of cited sources.
    from .core import digest
    unique = {digest(r): r for r in requirements}
    return {"_protected_all": list(unique.values()), "value": value}
