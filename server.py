from __future__ import annotations

import json
import os
import re
import threading
import urllib.error
import urllib.request
import uuid
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlparse


ROOT = Path(__file__).resolve().parent
HTML_PATH = ROOT / "citizen-agent-prototype.html"
SERVICES_PATH = ROOT / "data" / "services.json"
MOCK_CITIZEN_PATH = ROOT / "data" / "citizen-newborn-mock.json"
USER_PROFILES_DIR = ROOT / "data" / "users"


class AgentError(Exception):
    pass


class ModelAdapter(Protocol):
    def interpret(self, message: str, context: dict[str, Any]) -> dict[str, Any]:
        """Turn a citizen utterance into one constrained workflow action."""


class UserProfileStore(Protocol):
    def authenticate(self, username: str, password: str) -> dict[str, Any]:
        """Validate Demo credentials and return the username-scoped profile."""

    def issue_credentials(
        self,
        username: str,
        applications: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Persist credentials issued together from one consolidated request."""

    def record_usage(
        self, username: str, evidence_ids: list[str], service_id: str
    ) -> dict[str, Any]:
        """Record where persisted credentials were used."""

    def revoke_authorization(
        self, username: str, evidence_id: str
    ) -> dict[str, Any]:
        """Revoke this Agent's persisted authorization for one credential."""


class FileUserProfileStore:
    USERNAME_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,40}$")

    def __init__(self, directory: Path, fixture: dict[str, Any]) -> None:
        self._directory = directory
        self._fixture = fixture
        self._lock = threading.Lock()
        self._directory.mkdir(parents=True, exist_ok=True)

    def authenticate(self, username: str, password: str) -> dict[str, Any]:
        username = username.strip()
        if not self.USERNAME_PATTERN.fullmatch(username) or password != "password":
            raise AgentError("Demo 帳號或密碼錯誤")
        with self._lock:
            profile = self._load(username)
            if profile is None:
                profile = self._default_profile(username)
                self._write(profile)
            elif self._migrate_profile(profile):
                self._write(profile)
            return profile

    def issue_credentials(
        self,
        username: str,
        applications: list[dict[str, Any]],
    ) -> dict[str, Any]:
        with self._lock:
            profile = self._require_profile(username)
            issued_at = date.today()
            for application in applications:
                evidence_id = application["evidenceId"]
                profile["credentials"][evidence_id] = {
                    "reference": f"CRED-{evidence_id.upper()}-{uuid.uuid4().hex[:8].upper()}",
                    "acquiredAt": issued_at.isoformat(),
                    "expiresAt": (
                        issued_at + timedelta(days=application["validityDays"])
                    ).isoformat(),
                    "lastUsedAt": None,
                    "usedForServices": [],
                    "authorizedForAgent": False,
                    "applicationData": application["applicationData"],
                }
            self._write(profile)
            return profile

    def record_usage(
        self, username: str, evidence_ids: list[str], service_id: str
    ) -> dict[str, Any]:
        with self._lock:
            profile = self._require_profile(username)
            used_at = datetime.now().isoformat(timespec="seconds")
            for evidence_id in evidence_ids:
                credential = profile["credentials"].get(evidence_id)
                if not credential:
                    continue
                credential["lastUsedAt"] = used_at
                credential["authorizedForAgent"] = True
                if service_id not in credential["usedForServices"]:
                    credential["usedForServices"].append(service_id)
            self._write(profile)
            return profile

    def revoke_authorization(
        self, username: str, evidence_id: str
    ) -> dict[str, Any]:
        with self._lock:
            profile = self._require_profile(username)
            credential = profile["credentials"].get(evidence_id)
            if not credential:
                raise AgentError("Credential does not exist in this wallet")
            credential["authorizedForAgent"] = False
            credential["revokedAt"] = datetime.now().isoformat(timespec="seconds")
            self._write(profile)
            return profile

    def _default_profile(self, username: str) -> dict[str, Any]:
        return {
            "profileVersion": 2,
            "username": username,
            "displayName": self._fixture["displayName"],
            "credentials": {},
        }

    def _migrate_profile(self, profile: dict[str, Any]) -> bool:
        changed = False
        if profile.get("displayName") != self._fixture["displayName"]:
            profile["displayName"] = self._fixture["displayName"]
            changed = True
        if profile.get("profileVersion", 1) < 2:
            profile["credentials"] = {
                evidence_id: credential
                for evidence_id, credential in profile.get("credentials", {}).items()
                if str(credential.get("reference", "")).startswith("CRED-")
            }
            profile["profileVersion"] = 2
            changed = True
        for evidence_id, credential in profile.get("credentials", {}).items():
            if "authorizedForAgent" in credential:
                continue
            record = self._fixture["records"].get(evidence_id, {})
            credential["authorizedForAgent"] = bool(
                credential.get("lastUsedAt") or record.get("authorizedForAgent", False)
            )
            changed = True
        return changed

    def _path(self, username: str) -> Path:
        if not self.USERNAME_PATTERN.fullmatch(username):
            raise AgentError("Invalid username")
        return self._directory / f"{username}.json"

    def _load(self, username: str) -> dict[str, Any] | None:
        path = self._path(username)
        if not path.exists():
            return None
        with path.open(encoding="utf-8") as handle:
            return json.load(handle)

    def _require_profile(self, username: str) -> dict[str, Any]:
        profile = self._load(username)
        if profile is None:
            raise AgentError("User profile does not exist")
        return profile

    def _write(self, profile: dict[str, Any]) -> None:
        path = self._path(profile["username"])
        temporary = path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(profile, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(path)


class GovernmentDataAdapter(Protocol):
    def collect_evidence(
        self, subject_id: str, evidence_definitions: list[dict[str, Any]]
    ) -> dict[str, dict[str, Any]]:
        """Return the minimum government evidence required by one service."""

    def credential_inventory(self, subject_id: str) -> list[dict[str, Any]]:
        """Return government-issued credentials and their current usability."""

    def registered_bank_accounts(self, subject_id: str) -> list[dict[str, Any]]:
        """Return previously verified payment accounts."""

    def cases(
        self, subject_id: str, case_id: str | None = None
    ) -> list[dict[str, Any]]:
        """Return the citizen's submitted government cases."""


class MockGovernmentDataAdapter:
    def __init__(self, fixture: dict[str, Any]) -> None:
        self._fixture = fixture

    def collect_evidence(
        self, subject_id: str, evidence_definitions: list[dict[str, Any]]
    ) -> dict[str, dict[str, Any]]:
        self._require_subject(subject_id)
        records = self._fixture["records"]
        result: dict[str, dict[str, Any]] = {}
        for definition in evidence_definitions:
            if definition.get("source") != "government":
                raise AgentError("Government adapter received citizen-provided evidence")
            evidence_id = definition["id"]
            if evidence_id not in records:
                raise AgentError(f"Mock government record is missing: {evidence_id}")
            record = records[evidence_id]
            result[evidence_id] = {
                "label": record["label"],
                "issuer": record["issuer"],
                "recordId": record["recordId"],
                "lastUpdated": record["lastUpdated"],
                "expiresAt": record["expiresAt"],
                "satisfied": record["satisfied"],
                "disclosedValue": record["disclosedValue"],
            }
        return result

    def credential_inventory(self, subject_id: str) -> list[dict[str, Any]]:
        self._require_subject(subject_id)
        inventory: list[dict[str, Any]] = []
        for evidence_id, record in self._fixture["records"].items():
            status = (
                "expired"
                if record.get("recordStatus") == "expired"
                else "available"
                if record.get("authorizedForAgent")
                else "unauthorized"
            )
            inventory.append(
                {
                    "id": evidence_id,
                    "label": record["label"],
                    "credentialType": record["credentialType"],
                    "issuer": record["issuer"],
                    "expiresAt": record["expiresAt"],
                    "applicationInstructions": record["applicationInstructions"],
                    "applicationFields": record["applicationFields"],
                    "validityDays": record["validityDays"],
                    "status": status,
                }
            )
        return inventory

    def registered_bank_accounts(self, subject_id: str) -> list[dict[str, Any]]:
        self._require_subject(subject_id)
        return [dict(item) for item in self._fixture["registeredBankAccounts"]]

    def cases(
        self, subject_id: str, case_id: str | None = None
    ) -> list[dict[str, Any]]:
        self._require_subject(subject_id)
        cases = self._fixture.get("cases", [])
        return [
            dict(item)
            for item in cases
            if case_id is None or item["id"].lower() == case_id.lower()
        ]

    def fixture_view(self) -> dict[str, Any]:
        return self._fixture

    def _require_subject(self, subject_id: str) -> None:
        if subject_id != self._fixture["subjectId"]:
            raise AgentError("Unknown mock citizen")


class MockModelAdapter:
    def interpret(self, message: str, context: dict[str, Any]) -> dict[str, Any]:
        text = message.strip()
        state = context["state"]

        if state == "awaiting_question":
            if any(word in text for word in ("案件", "進度", "審核", "補件")):
                return {"action": "query_case"}
            event_matches = [
                (
                    ("生小孩", "新生兒", "育兒", "托育", "生育"),
                    "newborn-family",
                    {
                        "childcare-benefit-2026",
                        "birth-payment-2026",
                        "care-service-2026",
                    },
                    "了解，這看起來與新生兒或育兒服務有關。我會整理可能適用的項目。",
                ),
                (
                    ("生病", "住院", "醫療", "看病"),
                    "medical-hardship",
                    {"medical-hardship-2026"},
                    "了解，這看起來與醫療支出或急難救助有關。",
                ),
                (
                    ("租屋", "房租", "搬家", "租金"),
                    "housing",
                    {"rental-subsidy-2026"},
                    "了解，這看起來與租屋及住宅補助有關。",
                ),
                (
                    ("失業", "被裁員", "離職", "找工作"),
                    "unemployment",
                    {"unemployment-benefit-2026"},
                    "了解，這看起來與失業及就業保險給付有關。",
                ),
            ]
            for keywords, life_event, service_ids, assistant_message in event_matches:
                if not any(word in text for word in keywords):
                    continue
                return {
                    "action": "ask_question",
                    "message": text,
                    "lifeEvent": life_event,
                    "serviceIds": [
                        item["id"]
                        for item in context["services"]
                        if item["id"] in service_ids
                    ],
                    "assistantMessage": assistant_message,
                }
            return {
                "action": "message",
                "assistantMessage": "目前 Demo 支援新生兒、醫療急難、租金補貼、失業給付與案件進度查詢。請描述您遇到的情況。",
                "noServiceMatch": True,
            }
        if state == "awaiting_service":
            if len(context["services"]) == 1:
                return {
                    "action": "select_service",
                    "serviceId": context["services"][0]["id"],
                }
            if any(word in text for word in ("差別", "說明", "哪個", "比較")):
                return {
                    "action": "message",
                    "assistantMessage": "育兒生活補助是定期支持；生育給付是一次性給付；托育服務補助則與合格托育服務相關。您想先處理哪一項？",
                }
            if "生育" in text and "育兒" not in text:
                return {"action": "select_service", "serviceId": "birth-payment-2026"}
            if "托育" in text or "第三" in text:
                return {"action": "select_service", "serviceId": "care-service-2026"}
            if "第二" in text:
                return {"action": "select_service", "serviceId": "birth-payment-2026"}
            return {"action": "select_service", "serviceId": "childcare-benefit-2026"}
        if state == "awaiting_consent":
            if any(word in text for word in ("不同意", "拒絕", "不要")):
                return {"action": "deny_consent"}
            if any(word in text for word in ("為什麼", "哪些資料", "用途", "保存")):
                return {
                    "action": "message",
                    "assistantMessage": "資料只用於這次資格檢查，後端取得的是符合與否等最小結果，不會把完整戶籍、所得或身分資料交給對話模型。",
                }
            return {"action": "grant_consent"}
        if state == "awaiting_credentials":
            return {
                "action": "message",
                "assistantMessage": "請使用畫面中的憑證欄位，提供缺少憑證的識別資訊與有效期限。",
            }
        if state == "awaiting_documents":
            return {
                "action": "message",
                "assistantMessage": "請使用畫面中的上傳欄位提供缺少的文件；文件內容不會交給對話模型。",
            }
        if state == "eligible":
            if any(word in text for word in ("為什麼", "依據", "怎麼判斷")):
                return {
                    "action": "message",
                    "assistantMessage": "資格結果由版本化規則引擎根據已授權的政府證據計算，不是由語言模型猜測。",
                }
            return {"action": "create_draft"}
        if state == "awaiting_account":
            if "郵局" in text or "5678" in text or "第二" in text:
                return {"action": "select_bank_account", "accountId": "bank-tax"}
            return {"action": "select_bank_account", "accountId": "bank-labor"}
        if state == "awaiting_confirmation":
            if any(word in text for word in ("確認", "同意", "送出", "可以", "是")):
                return {"action": "confirm_and_submit"}
            return {
                "action": "message",
                "assistantMessage": "申請尚未送出。您可以更換帳戶，或明確回覆確認後再送件。",
            }
        if state == "submitted" and any(
            word in text for word in ("案件", "進度", "審核", "補件")
        ):
            return {"action": "query_case"}
        return {"action": "message", "assistantMessage": "您可以繼續查詢案件進度。"}


class OpenAICompatibleModelAdapter:
    def __init__(self) -> None:
        self.api_key = os.environ.get("MODEL_API_KEY", "")
        self.base_url = os.environ.get("MODEL_BASE_URL", "https://api.openai.com/v1").rstrip("/")
        self.model = os.environ.get("MODEL_NAME", "gpt-4.1-mini")
        if not self.api_key:
            raise AgentError("MODEL_API_KEY is required when AGENT_MODEL_PROVIDER=openai-compatible")

    def interpret(self, message: str, context: dict[str, Any]) -> dict[str, Any]:
        state = context["state"]
        allowed = {
            "awaiting_question": ["ask_question", "query_case", "message"],
            "awaiting_service": ["select_service", "message"],
            "awaiting_credentials": ["message"],
            "awaiting_consent": ["grant_consent", "deny_consent", "message"],
            "awaiting_documents": ["message"],
            "eligible": ["create_draft", "message"],
            "ineligible": ["message"],
            "awaiting_account": ["select_bank_account", "add_bank_account", "message"],
            "awaiting_confirmation": ["confirm_and_submit", "message"],
            "submitted": ["query_case", "message"],
        }.get(state, ["message"])

        model_context = {
            "state": state,
            "allowedActions": allowed,
            "conversation": context.get("conversation", []),
            "services": context.get("services", []),
            "selectedService": context.get("selectedService"),
            "uploadedDocuments": context.get("uploadedDocuments", []),
            "bankAccounts": context.get("bankAccounts", []),
        }
        body = {
            "model": self.model,
            "response_format": {"type": "json_object"},
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You route a citizen through a government-service workflow. "
                        "Return one JSON object with action and only the parameters required "
                        "for that action. Use assistantMessage when action is message. "
                        "When action is ask_question, include assistantMessage, lifeEvent, "
                        "and serviceIds selected only from Context.services. If no listed "
                        "service matches, use action=message and noServiceMatch=true. "
                        "Use query_case for requests about an existing application, review "
                        "status, missing documents, or case progress; include caseId only "
                        "when the citizen supplied one. "
                        "Never invent eligibility, consent, government records, service IDs, "
                        "bank accounts, or submission success. Ask or explain with action=message "
                        "when the citizen is ambiguous. Context:\n"
                        f"{json.dumps(model_context, ensure_ascii=False)}"
                    ),
                },
                {"role": "user", "content": message},
            ],
        }
        request = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                payload = json.load(response)
        except (urllib.error.URLError, TimeoutError) as exc:
            raise AgentError(f"Model request failed: {exc}") from exc

        try:
            result = json.loads(payload["choices"][0]["message"]["content"])
        except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
            raise AgentError("Model returned an invalid structured response") from exc

        if result.get("action") not in allowed:
            raise AgentError("Model selected an action that is illegal in the current state")
        return result


class AzureFoundryResponsesModelAdapter:
    def __init__(self) -> None:
        self.api_key = os.environ.get("MODEL_API_KEY", "")
        self.endpoint = os.environ.get("MODEL_ENDPOINT", "")
        self.model = os.environ.get("MODEL_NAME", "")
        if not self.api_key:
            raise AgentError(
                "MODEL_API_KEY is required when AGENT_MODEL_PROVIDER=azure-foundry-responses"
            )
        if not self.endpoint:
            raise AgentError(
                "MODEL_ENDPOINT is required when AGENT_MODEL_PROVIDER=azure-foundry-responses"
            )
        if not self.model:
            raise AgentError(
                "MODEL_NAME must contain the Azure model deployment name"
            )
        if not self.endpoint.rstrip("/").endswith("/responses"):
            self.endpoint = f"{self.endpoint.rstrip('/')}/responses"

    def interpret(self, message: str, context: dict[str, Any]) -> dict[str, Any]:
        state = context["state"]
        allowed = {
            "awaiting_question": ["ask_question", "query_case", "message"],
            "awaiting_service": ["select_service", "message"],
            "awaiting_credentials": ["message"],
            "awaiting_consent": ["grant_consent", "deny_consent", "message"],
            "awaiting_documents": ["message"],
            "eligible": ["create_draft", "message"],
            "ineligible": ["message"],
            "awaiting_account": [
                "select_bank_account",
                "add_bank_account",
                "message",
            ],
            "awaiting_confirmation": ["confirm_and_submit", "message"],
            "submitted": ["query_case", "message"],
        }.get(state, ["message"])
        model_context = {
            "state": state,
            "allowedActions": allowed,
            "conversation": context.get("conversation", []),
            "services": context.get("services", []),
            "selectedService": context.get("selectedService"),
            "uploadedDocuments": context.get("uploadedDocuments", []),
            "bankAccounts": context.get("bankAccounts", []),
        }
        instructions = (
            "You route a citizen through a government-service workflow. "
            "Return exactly one JSON object. It must contain action and only the "
            "parameters needed by that action. Use assistantMessage when action is "
            "message. When action is ask_question, include assistantMessage, lifeEvent, "
            "and serviceIds selected only from Context.services. If no listed service "
            "matches the citizen's need, use action=message, noServiceMatch=true, and "
            "explain the current scope. "
            "Use action=query_case when the citizen asks about an existing application, "
            "review status, missing documents, or progress. Include caseId only if stated. "
            "Never invent eligibility, consent, government records, service "
            "IDs, bank accounts, or submission success. If the citizen is ambiguous, "
            "explain or ask one concise question with action=message. "
            f"Context: {json.dumps(model_context, ensure_ascii=False)}"
        )
        body = {
            "model": self.model,
            "instructions": instructions,
            "input": message,
        }
        request = urllib.request.Request(
            self.endpoint,
            data=json.dumps(body).encode("utf-8"),
            headers={
                "api-key": self.api_key,
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                payload = json.load(response)
        except urllib.error.HTTPError as exc:
            error_body = exc.read().decode("utf-8", errors="replace")
            raise AgentError(
                f"Azure Foundry request failed with HTTP {exc.code}: {error_body[:300]}"
            ) from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise AgentError(f"Azure Foundry request failed: {exc}") from exc

        text = self._output_text(payload)
        try:
            result = json.loads(text)
        except json.JSONDecodeError as exc:
            raise AgentError("Azure Foundry model did not return valid JSON") from exc
        if result.get("action") not in allowed:
            raise AgentError(
                "Azure Foundry model selected an action that is illegal in the current state"
            )
        return result

    @staticmethod
    def _output_text(payload: dict[str, Any]) -> str:
        for output in payload.get("output", []):
            for content in output.get("content", []):
                if content.get("type") == "output_text" and content.get("text"):
                    return content["text"].strip()
        raise AgentError("Azure Foundry response did not contain output text")


@dataclass
class Message:
    actor: str
    text: str


@dataclass
class AuditEntry:
    actor: str
    action: str
    detail: str
    time: str = field(default_factory=lambda: datetime.now().strftime("%H:%M:%S"))


@dataclass
class Session:
    id: str
    username: str = "user"
    display_name: str = "牛來"
    subject_id: str = "citizen-demo-001"
    state: str = "awaiting_question"
    question: str = ""
    life_event: str = ""
    no_service_match: bool = False
    candidate_services: list[dict[str, Any]] = field(default_factory=list)
    selected_service: dict[str, Any] | None = None
    consent: str = "not_requested"
    evidence: dict[str, str] = field(default_factory=dict)
    evidence_details: dict[str, dict[str, Any]] = field(default_factory=dict)
    credential_inventory: list[dict[str, Any]] = field(default_factory=list)
    uploaded_documents: dict[str, dict[str, Any]] = field(default_factory=dict)
    eligibility: str = "not_checked"
    draft: dict[str, Any] | None = None
    bank_accounts: list[dict[str, Any]] = field(default_factory=list)
    selected_bank_account_id: str | None = None
    application: dict[str, Any] | None = None
    case_results: list[dict[str, Any]] = field(default_factory=list)
    messages: list[Message] = field(default_factory=list)
    audit: list[AuditEntry] = field(default_factory=list)


class CitizenAgent:
    """Deep workflow module used by both HTTP callers and tests."""

    def __init__(
        self,
        model: ModelAdapter,
        services: list[dict[str, Any]],
        government_data: GovernmentDataAdapter,
        user_profiles: UserProfileStore,
    ) -> None:
        self._model = model
        self._services = services
        self._government_data = government_data
        self._user_profiles = user_profiles
        self._sessions: dict[str, Session] = {}
        self._lock = threading.Lock()

    def login(self, username: str, password: str) -> dict[str, Any]:
        profile = self._user_profiles.authenticate(username, password)
        return self.start(profile)

    def start(self, profile: dict[str, Any] | None = None) -> dict[str, Any]:
        if profile is None:
            profile = self._user_profiles.authenticate("user", "password")
        session = Session(
            id=str(uuid.uuid4()),
            username=profile["username"],
            display_name=profile["displayName"],
            bank_accounts=self._government_data.registered_bank_accounts(
                "citizen-demo-001"
            ),
            credential_inventory=self._inventory_for_profile(
                profile,
                self._government_data.credential_inventory("citizen-demo-001"),
            ),
        )
        session.messages.append(
            Message(
                actor="agent",
                text=(
                    f"{profile['displayName']}您好，我可以協助您找出適用服務、檢查資格、準備申請，"
                    "也可以查詢既有案件進度。請告訴我最近發生了什麼事。"
                ),
            )
        )
        with self._lock:
            self._sessions[session.id] = session
        return self._view(session)

    @staticmethod
    def _inventory_for_profile(
        profile: dict[str, Any], catalog: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        inventory: list[dict[str, Any]] = []
        today = date.today().isoformat()
        for item in catalog:
            credential = profile["credentials"].get(item["id"])
            if credential is None or credential.get("revokedAt"):
                status = "unavailable"
            elif credential["expiresAt"] < today:
                status = "expired"
            elif credential.get("authorizedForAgent", False):
                status = "available"
            else:
                status = "unauthorized"
            inventory.append(
                {
                    **item,
                    "status": status,
                    "reference": credential["reference"] if credential else None,
                    "lastUsedAt": credential["lastUsedAt"] if credential else None,
                    "usedForServices": credential["usedForServices"] if credential else [],
                    "expiresAt": credential["expiresAt"] if credential else item["expiresAt"],
                }
            )
        return inventory

    def get(self, session_id: str) -> dict[str, Any]:
        return self._view(self._session(session_id))

    def send(
        self,
        session_id: str,
        message: str = "",
        action: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        session = self._session(session_id)
        command = (
            {"action": action, **(payload or {})}
            if action
            else self._model.interpret(message, self._model_context(session))
        )
        if message:
            command.setdefault("message", message)

        handler = getattr(self, f"_on_{command['action']}", None)
        if handler is None:
            raise AgentError(f"Unsupported action: {command['action']}")

        with self._lock:
            handler(session, command)
        return self._view(session)

    def _on_ask_question(self, session: Session, command: dict[str, Any]) -> None:
        self._require(session, "awaiting_question")
        message = command.get("message", "").strip()
        if not message:
            raise AgentError("A question is required")
        requested_ids = command.get("serviceIds", [])
        candidate_services = [
            service for service in self._services if service["id"] in requested_ids
        ]
        session.question = message
        session.messages.append(Message("user", message))
        if not candidate_services:
            session.no_service_match = True
            session.messages.append(
                Message(
                    "agent",
                    command.get("assistantMessage")
                    or "目前服務目錄中沒有符合這個需求的項目，請換一種方式描述。",
                )
            )
            session.audit.append(AuditEntry("Agent", "未找到服務", message))
            return

        session.life_event = command.get("lifeEvent", "")
        session.no_service_match = False
        session.candidate_services = candidate_services
        session.state = "awaiting_service"
        session.messages.extend(
            [
                Message(
                    "agent",
                    command.get("assistantMessage")
                    or "我正在整理可能適用的跨機關服務。",
                ),
                Message(
                    "agent",
                    f"我找到 {len(candidate_services)} 項可能適用的服務，請選擇想先處理的項目。",
                ),
            ]
        )
        session.audit.append(AuditEntry("民眾", "提出問題", message))

    def _on_select_service(self, session: Session, command: dict[str, Any]) -> None:
        self._require(session, "awaiting_service")
        service_id = command.get("serviceId")
        service = next((item for item in self._services if item["id"] == service_id), None)
        if not service:
            raise AgentError("Unknown service")
        session.selected_service = service
        session.evidence = {
            item["id"]: (
                self._initial_evidence_status(session, item["id"])
                if item["source"] == "government"
                else "upload_required"
            )
            for item in service["evidence"]
        }
        session.consent = "not_requested"
        government_evidence = [
            item for item in service["evidence"] if item["source"] == "government"
        ]
        upload_evidence = [
            item for item in service["evidence"] if item["source"] == "citizen_upload"
        ]
        government_lines: list[str] = []
        for item in government_evidence:
            status = session.evidence[item["id"]]
            if status in {"unavailable", "expired"}:
                catalog_item = next(
                    credential
                    for credential in session.credential_inventory
                    if credential["id"] == item["id"]
                )
                required_labels = "、".join(
                    field["label"] for field in catalog_item["applicationFields"]
                )
                government_lines.append(
                    (
                        f"• 尚無可用的「{item['label']}」；我會先向{item['department']}申請，"
                        f"需要您提供：{required_labels}"
                    )
                )
            else:
                government_lines.append(
                    (
                        f"• 數位憑證皮夾已有「{item['label']}」；同意後將使用此憑證"
                        f"向{item['department']}取得最小必要結果"
                    )
                )
        upload_lines = "\n".join(
            f"• {item['label']}：{item['instructions']}" for item in upload_evidence
        )
        plan_parts = [
            "我先檢查了您的數位憑證皮夾：",
            "\n".join(government_lines),
        ]
        if upload_lines:
            plan_parts.extend(
                [
                    "\n以下資料政府不一定持有，需要由您上傳：",
                    upload_lines,
                ]
            )
        missing_credentials = [
            item
            for item in government_evidence
            if session.evidence[item["id"]] in {"unavailable", "expired"}
        ]
        if missing_credentials:
            missing_lines = "\n".join(
                f"• {item['department']}：{item['label']}" for item in missing_credentials
            )
            plan_parts.extend(
                [
                    "\n您的數位憑證皮夾目前缺少或需要更新以下憑證：",
                    missing_lines,
                ]
            )
        plan_text = "\n".join(plan_parts)
        if missing_credentials:
            session.state = "awaiting_credentials"
            next_step = (
                "請使用下方欄位提供申請憑證所需資料。Demo 會把建立結果存入此帳號的本機數位憑證皮夾，供日後使用。"
            )
        else:
            session.state = "awaiting_consent"
            session.consent = "pending"
            next_step = "是否同意本 Agent 向上述部門取得本次所需資料？"
        session.messages.extend(
            [
                Message("user", command.get("message") or f"我想先申請「{service['name']}」。"),
                Message(
                    "agent",
                    (
                        f"好的，先處理「{service['name']}」，預估補助為{service['estimatedAmount']}。\n\n"
                        f"{plan_text}\n\n"
                        "政府資料只會用於本次資格檢查，且不會把完整原始資料提供給對話模型。"
                        f"{next_step}"
                    ),
                ),
            ]
        )
        departments = "、".join(
            dict.fromkeys(item["department"] for item in government_evidence)
        )
        session.audit.extend(
            [
                AuditEntry("民眾", "選擇政府服務", service["name"]),
                AuditEntry("Agent", "揭露資料來源", departments),
            ]
        )

    def _on_create_credentials(self, session: Session, command: dict[str, Any]) -> None:
        self._require(session, "awaiting_credentials")
        raw_application_data = command.get("applicationData")
        if not isinstance(raw_application_data, dict):
            raise AgentError("Credential application data is required")
        missing_definitions = [
            item
            for item in session.selected_service["evidence"]
            if item["source"] == "government"
            and self._credential_status(session, item["id"]) in {"unavailable", "expired"}
        ]
        if not missing_definitions:
            raise AgentError("There are no missing credentials to issue")

        applications: list[dict[str, Any]] = []
        for definition in missing_definitions:
            catalog_item = next(
                item
                for item in session.credential_inventory
                if item["id"] == definition["id"]
            )
            application_data: dict[str, str] = {}
            for field_definition in catalog_item["applicationFields"]:
                field_name = field_definition["name"]
                value = str(raw_application_data.get(field_name, "")).strip()
                if not value:
                    raise AgentError(f"{field_definition['label']} is required")
                application_data[field_name] = value
            applications.append(
                {
                    "evidenceId": definition["id"],
                    "applicationData": application_data,
                    "validityDays": int(catalog_item["validityDays"]),
                }
            )

        profile = self._user_profiles.issue_credentials(
            session.username, applications
        )
        catalog = self._government_data.credential_inventory(session.subject_id)
        session.credential_inventory = self._inventory_for_profile(profile, catalog)
        for definition in missing_definitions:
            session.evidence[definition["id"]] = "locked"
        issued_labels = "、".join(
            f"{item['department']}的{item['label']}" for item in missing_definitions
        )
        session.messages.extend(
            [
                Message(
                    "user",
                    "我已提供申請這些政府憑證需要的資料。",
                ),
                Message(
                    "agent",
                    (
                        f"已一次送出 {len(applications)} 筆 Mock 政府發證 API 請求，"
                        f"並取得：{issued_labels}。憑證已存入 {session.username} 的本機 Demo 數位憑證皮夾。"
                    ),
                ),
            ]
        )
        session.audit.extend(
            AuditEntry(
                item["department"],
                "核發政府憑證",
                (
                    f"{item['label']}；有效至 "
                    f"{profile['credentials'][item['id']]['expiresAt']}"
                ),
            )
            for item in missing_definitions
        )
        session.state = "awaiting_consent"
        session.consent = "pending"
        departments = "、".join(
            dict.fromkeys(
                item["department"]
                for item in session.selected_service["evidence"]
                if item["source"] == "government"
            )
        )
        session.messages.append(
            Message(
                "agent",
                f"所需憑證已備妥。是否同意本 Agent 向{departments}取得本次資格檢查資料？",
            )
        )

    def _on_revoke_credential_authorization(
        self, session: Session, command: dict[str, Any]
    ) -> None:
        evidence_id = str(command.get("evidenceId", ""))
        credential = next(
            (
                item
                for item in session.credential_inventory
                if item["id"] == evidence_id
            ),
            None,
        )
        if not credential:
            raise AgentError("Unknown government credential")
        if credential["status"] != "available":
            raise AgentError("This credential is not currently authorized")
        profile = self._user_profiles.revoke_authorization(
            session.username, evidence_id
        )
        session.credential_inventory = self._inventory_for_profile(
            profile,
            self._government_data.credential_inventory(session.subject_id),
        )
        if session.selected_service and any(
            item["id"] == evidence_id
            for item in session.selected_service["evidence"]
        ):
            session.evidence[evidence_id] = "locked"
            session.evidence_details.pop(evidence_id, None)
            if session.state != "submitted":
                session.consent = "pending"
                session.eligibility = "not_checked"
                session.draft = None
                session.selected_bank_account_id = None
                session.state = "awaiting_consent"
        session.messages.append(
            Message(
                "agent",
                (
                    f"已撤銷「{credential['credentialType']}」。此憑證目前不可供 Agent 使用，"
                    "下次需要時必須重新提供資料並向發證機關申請。"
                ),
            )
        )
        session.audit.append(
            AuditEntry(
                "民眾",
                "撤銷 Agent 憑證授權",
                f"{credential['issuer']}：{credential['credentialType']}",
            )
        )

    def _on_grant_consent(self, session: Session, command: dict[str, Any]) -> None:
        self._require(session, "awaiting_consent")
        session.messages.append(Message("user", command.get("message") or "我同意本次資格檢查。"))
        session.consent = "granted"
        service = session.selected_service
        government_evidence = [
            item for item in service["evidence"] if item["source"] == "government"
        ]
        expired = [
            item
            for item in government_evidence
            if self._credential_status(session, item["id"]) == "expired"
        ]
        if expired:
            labels = "、".join(item["label"] for item in expired)
            session.eligibility = "blocked"
            session.state = "ineligible"
            session.messages.append(
                Message("agent", f"無法繼續資格檢查：{labels}已過期，請先向發證機關更新。")
            )
            session.audit.append(AuditEntry("政策閘門", "阻擋過期憑證", labels))
            return
        unavailable = [
            item
            for item in government_evidence
            if self._credential_status(session, item["id"]) == "unavailable"
        ]
        if unavailable:
            labels = "、".join(item["label"] for item in unavailable)
            session.eligibility = "blocked"
            session.state = "ineligible"
            session.messages.append(
                Message("agent", f"無法繼續資格檢查：目前沒有可用的{labels}。")
            )
            session.audit.append(AuditEntry("政策閘門", "阻擋缺少憑證", labels))
            return

        requested_ids = {item["id"] for item in government_evidence}
        profile = self._user_profiles.record_usage(
            session.username,
            list(requested_ids),
            service["id"],
        )
        session.credential_inventory = self._inventory_for_profile(
            profile,
            self._government_data.credential_inventory(session.subject_id),
        )
        for credential in session.credential_inventory:
            if credential["id"] in requested_ids:
                credential["status"] = "available"
        session.evidence_details = self._government_data.collect_evidence(
            session.subject_id, government_evidence
        )
        for evidence_id in session.evidence_details:
            session.evidence[evidence_id] = "available"
        departments = "、".join(
            dict.fromkeys(item["department"] for item in government_evidence)
        )
        session.audit.append(
            AuditEntry(
                "民眾",
                "授予資料存取",
                f"一次性、限{service['name']}資格檢查；部門：{departments}",
            )
        )
        pending_uploads = [
            item
            for item in service["evidence"]
            if item["source"] == "citizen_upload"
            and session.evidence[item["id"]] != "uploaded"
        ]
        if pending_uploads:
            session.state = "awaiting_documents"
            labels = "、".join(item["label"] for item in pending_uploads)
            session.messages.append(
                Message(
                    "agent",
                    f"政府憑證已取得。還需要您提供：{labels}。請使用下方上傳欄位，文件內容不會提供給對話模型。",
                )
            )
            return

        self._complete_eligibility(session)

    def _on_upload_document(self, session: Session, command: dict[str, Any]) -> None:
        self._require(session, "awaiting_documents")
        evidence_id = str(command.get("evidenceId", ""))
        definition = next(
            (
                item
                for item in session.selected_service["evidence"]
                if item["id"] == evidence_id and item["source"] == "citizen_upload"
            ),
            None,
        )
        if not definition:
            raise AgentError("Unknown citizen-provided document")
        file_name = str(command.get("fileName", "")).strip()
        content_type = str(command.get("contentType", "")).strip() or "application/octet-stream"
        size = int(command.get("size", 0))
        if not file_name or size <= 0:
            raise AgentError("A non-empty document is required")
        if size > 10 * 1024 * 1024:
            raise AgentError("Document exceeds the 10 MB demo limit")
        suffix = Path(file_name).suffix.lower()
        if suffix not in definition["acceptedTypes"]:
            raise AgentError(
                f"Unsupported document type; accepted: {', '.join(definition['acceptedTypes'])}"
            )
        document = {
            "evidenceId": evidence_id,
            "label": definition["label"],
            "fileName": file_name,
            "contentType": content_type,
            "size": size,
            "uploadedAt": datetime.now().isoformat(timespec="seconds"),
            "satisfied": True,
            "disclosedValue": f"民眾已提供 {file_name}",
        }
        session.uploaded_documents[evidence_id] = document
        session.evidence_details[evidence_id] = document
        session.evidence[evidence_id] = "uploaded"
        session.messages.extend(
            [
                Message("user", f"我已上傳「{definition['label']}」：{file_name}"),
                Message(
                    "agent",
                    f"已收到「{definition['label']}」。Demo 只保留檔名、類型與大小，不會把文件內容交給對話模型。",
                ),
            ]
        )
        session.audit.append(
            AuditEntry("民眾", "上傳申請文件", f"{definition['label']}：{file_name}")
        )
        remaining = [
            item
            for item in session.selected_service["evidence"]
            if item["source"] == "citizen_upload"
            and session.evidence[item["id"]] != "uploaded"
        ]
        if remaining:
            labels = "、".join(item["label"] for item in remaining)
            session.messages.append(Message("agent", f"仍待提供：{labels}。"))
            return
        self._complete_eligibility(session)

    def _complete_eligibility(self, session: Session) -> None:
        service = session.selected_service
        eligible = all(
            session.evidence.get(item["id"]) in {"available", "uploaded"}
            and session.evidence_details[item["id"]]["satisfied"]
            for item in service["evidence"]
        )
        session.eligibility = "eligible" if eligible else "not_eligible"
        session.state = "eligible" if eligible else "ineligible"
        labels = "、".join(item["label"] for item in service["evidence"])
        session.messages.append(
            Message(
                "agent",
                (
                    f"資格檢查完成：您目前符合「{service['name']}」條件。\n\n"
                    f"判斷依據：{labels}皆符合。"
                    if eligible
                    else f"資格檢查完成：您目前不符合「{service['name']}」條件。"
                ),
            )
        )
        session.audit.append(
            AuditEntry(
                "規則引擎",
                "資格判斷",
                f"{'符合' if eligible else '不符合'}；規則版本 2026.08",
            )
        )

    def _on_deny_consent(self, session: Session, command: dict[str, Any]) -> None:
        self._require(session, "awaiting_consent")
        session.consent = "denied"
        session.eligibility = "blocked"
        session.messages.extend(
            [
                Message("user", command.get("message") or "我不同意調閱資料。"),
                Message("agent", "沒問題。我不會調閱資料，仍可提供一般申請條件。"),
            ]
        )
        session.audit.append(AuditEntry("民眾", "拒絕資料存取", "未調閱任何資料"))

    def _on_create_draft(self, session: Session, command: dict[str, Any]) -> None:
        self._require(session, "eligible")
        service = session.selected_service
        session.messages.append(Message("user", command.get("message") or "請幫我準備申請草稿。"))
        review_evidence = []
        for definition in service["evidence"]:
            detail = session.evidence_details[definition["id"]]
            review_evidence.append(
                {
                    "label": definition["label"],
                    "source": (
                        definition["department"]
                        if definition["source"] == "government"
                        else "民眾上傳"
                    ),
                    "value": detail["disclosedValue"],
                }
            )
        session.draft = {
            "service": service["name"],
            "agency": service["agency"],
            "applicant": session.display_name,
            "evidenceCount": len(service["evidence"]),
            "reviewEvidence": review_evidence,
            "paymentAccount": "尚未選擇",
            "status": "等待確認",
        }
        session.state = "awaiting_account"
        session.messages.append(
            Message(
                "agent",
                (
                    "申請草稿已完成。下方 Review 會列出準備送給政府的所有資料，"
                    "目前尚未送件。確認內容後，請選擇入帳帳戶。"
                ),
            )
        )
        session.audit.append(AuditEntry("Agent", "建立申請草稿", "尚未送件"))

    def _on_select_bank_account(self, session: Session, command: dict[str, Any]) -> None:
        self._require(session, "awaiting_account")
        account_id = command.get("accountId")
        account = next((item for item in session.bank_accounts if item["id"] == account_id), None)
        if not account:
            raise AgentError("Unknown bank account")
        session.selected_bank_account_id = account_id
        session.draft["paymentAccount"] = (
            f"{account['bank']} {account['maskedNumber']}（登記於{account['source']}）"
        )
        session.state = "awaiting_confirmation"
        session.messages.extend(
            [
                Message(
                    "user",
                    command.get("message")
                    or f"我要使用{account['bank']} {account['maskedNumber']}。",
                ),
                Message(
                    "agent",
                    (
                        f"您選擇的是 {account['bank']} {account['maskedNumber']}，"
                        f"曾登記於「{account['source']}」。確認後我會送出申請，是否確認？"
                    ),
                ),
            ]
        )
        session.audit.append(
            AuditEntry("民眾", "選擇入帳帳戶", f"{account['bank']}；來源：{account['source']}")
        )

    def _on_add_bank_account(self, session: Session, command: dict[str, Any]) -> None:
        self._require(session, "awaiting_account")
        bank = str(command.get("bank", "")).strip()
        number = "".join(str(command.get("number", "")).split())
        if not bank or len(number) < 6:
            raise AgentError("Bank name and full account number are required")
        account = {
            "id": f"bank-new-{uuid.uuid4().hex[:8]}",
            "bank": bank,
            "maskedNumber": f"•••• {number[-4:]}",
            "source": "本次由民眾新增",
            "verified": False,
        }
        session.bank_accounts.append(account)
        session.selected_bank_account_id = account["id"]
        session.draft["paymentAccount"] = (
            f"{account['bank']} {account['maskedNumber']}（等待驗證）"
        )
        session.messages.extend(
            [
                Message("user", f"我要改用新的{bank}帳戶 {account['maskedNumber']}。"),
                Message("agent", "新帳戶需要先完成帳戶持有人驗證，才能用於本次申請。"),
            ]
        )
        session.audit.append(AuditEntry("民眾", "新增入帳帳戶", f"{bank}；等待驗證"))

    def _on_verify_bank_account(self, session: Session, command: dict[str, Any]) -> None:
        self._require(session, "awaiting_account")
        account = next(
            (
                item
                for item in session.bank_accounts
                if item["id"] == session.selected_bank_account_id
            ),
            None,
        )
        if not account or account["verified"]:
            raise AgentError("There is no new account waiting for verification")
        account["verified"] = True
        account["source"] = "本次申請完成帳戶持有人驗證"
        session.draft["paymentAccount"] = (
            f"{account['bank']} {account['maskedNumber']}（本次已驗證）"
        )
        session.state = "awaiting_confirmation"
        session.messages.append(
            Message(
                "agent",
                (
                    f"帳戶持有人驗證完成。您選擇的是 {account['bank']} "
                    f"{account['maskedNumber']}。確認後我會送出申請，是否確認？"
                ),
            )
        )
        session.audit.append(
            AuditEntry("帳戶驗證服務", "驗證新帳戶", f"{account['bank']} {account['maskedNumber']}")
        )

    def _on_confirm_and_submit(self, session: Session, command: dict[str, Any]) -> None:
        self._require(session, "awaiting_confirmation")
        session.messages.append(
            Message("user", command.get("message") or "確認，請使用這個帳戶送出申請。")
        )
        session.application = {
            "id": "APP-2026-0829-001",
            "status": "已收件",
        }
        session.draft["status"] = "已送出"
        session.state = "submitted"
        session.messages.append(
            Message(
                "agent",
                "申請已成功送出。\n案件編號：APP-2026-0829-001\n目前狀態：已收件",
            )
        )
        session.audit.extend(
            [
                AuditEntry("民眾", "確認申請內容", "允許送出此一申請"),
                AuditEntry("政府服務介面", "受理申請", "APP-2026-0829-001"),
            ]
        )

    def _on_message(self, session: Session, command: dict[str, Any]) -> None:
        message = command.get("message", "").strip()
        if message:
            session.messages.append(Message("user", message))
        session.messages.append(
            Message(
                "agent",
                command.get("assistantMessage")
                or "我目前無法確認您的意思，請換一種方式描述。",
            )
        )
        session.no_service_match = bool(command.get("noServiceMatch", False))
        session.audit.append(
            AuditEntry(
                "Agent",
                "未找到相符服務" if session.no_service_match else "回覆補充問題",
                message or "Model 產生一般說明",
            )
        )

    def _on_query_case(self, session: Session, command: dict[str, Any]) -> None:
        message = command.get("message", "").strip()
        if message:
            session.messages.append(Message("user", message))
        case_id = command.get("caseId")
        session.case_results = self._government_data.cases(
            session.subject_id, case_id
        )
        session.no_service_match = False
        if not session.case_results:
            session.messages.append(
                Message(
                    "agent",
                    "找不到符合的案件。請確認案件編號，或改為查詢全部案件。",
                )
            )
            detail = case_id or "全部案件"
            session.audit.append(AuditEntry("Agent", "案件查詢無結果", detail))
            return

        if len(session.case_results) == 1:
            item = session.case_results[0]
            summary = (
                f"{item['serviceName']}（{item['id']}）目前狀態為「{item['status']}」。"
                f"\n下一步：{item['nextStep']}"
            )
        else:
            summary = (
                f"目前找到 {len(session.case_results)} 筆案件，"
                "請查看下方案件卡片中的最新狀態與下一步。"
            )
        session.messages.append(Message("agent", summary))
        session.audit.append(
            AuditEntry(
                "政府案件系統",
                "查詢案件進度",
                case_id or f"全部 {len(session.case_results)} 筆案件",
            )
        )

    def _model_context(self, session: Session) -> dict[str, Any]:
        visible_services = (
            self._services
            if session.state == "awaiting_question"
            else session.candidate_services
        )
        return {
            "state": session.state,
            "conversation": [
                {"actor": item.actor, "text": item.text}
                for item in session.messages[-8:]
            ],
            "services": [
                {
                    "id": service["id"],
                    "name": service["name"],
                    "summary": service["summary"],
                    "estimatedAmount": service["estimatedAmount"],
                }
                for service in visible_services
            ],
            "selectedService": (
                {
                    "id": session.selected_service["id"],
                    "name": session.selected_service["name"],
                    "evidence": session.selected_service["evidence"],
                }
                if session.selected_service
                else None
            ),
            "uploadedDocuments": list(session.uploaded_documents.values()),
            "bankAccounts": [
                {
                    "id": account["id"],
                    "bank": account["bank"],
                    "maskedNumber": account["maskedNumber"],
                    "source": account["source"],
                    "verified": account["verified"],
                }
                for account in session.bank_accounts
                if session.draft
            ],
            "capabilities": ["service_discovery", "eligibility", "application", "case_query"],
        }

    def _session(self, session_id: str) -> Session:
        try:
            return self._sessions[session_id]
        except KeyError as exc:
            raise AgentError("Unknown session") from exc

    @staticmethod
    def _credential_status(session: Session, evidence_id: str) -> str:
        credential = next(
            (
                item
                for item in session.credential_inventory
                if item["id"] == evidence_id
            ),
            None,
        )
        return credential["status"] if credential else "unavailable"

    @classmethod
    def _initial_evidence_status(cls, session: Session, evidence_id: str) -> str:
        credential_status = cls._credential_status(session, evidence_id)
        return credential_status if credential_status in {"expired", "unavailable"} else "locked"

    @staticmethod
    def _require(session: Session, expected: str) -> None:
        if session.state != expected:
            raise AgentError(f"Action is not allowed while session state is {session.state}")

    def _view(self, session: Session) -> dict[str, Any]:
        data = asdict(session)
        data["bankAccounts"] = session.bank_accounts if session.draft else []
        return data


def load_services() -> list[dict[str, Any]]:
    with SERVICES_PATH.open(encoding="utf-8") as handle:
        return json.load(handle)


def load_mock_citizen() -> dict[str, Any]:
    with MOCK_CITIZEN_PATH.open(encoding="utf-8") as handle:
        return json.load(handle)


def create_model() -> ModelAdapter:
    provider = os.environ.get("AGENT_MODEL_PROVIDER", "mock")
    if provider == "mock":
        return MockModelAdapter()
    if provider == "openai-compatible":
        return OpenAICompatibleModelAdapter()
    if provider == "azure-foundry-responses":
        return AzureFoundryResponsesModelAdapter()
    raise AgentError(f"Unknown AGENT_MODEL_PROVIDER: {provider}")


GOVERNMENT_DATA = MockGovernmentDataAdapter(load_mock_citizen())
USER_PROFILES = FileUserProfileStore(USER_PROFILES_DIR, load_mock_citizen())
AGENT = CitizenAgent(create_model(), load_services(), GOVERNMENT_DATA, USER_PROFILES)


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/":
            self._send_bytes(HTTPStatus.OK, HTML_PATH.read_bytes(), "text/html; charset=utf-8")
            return
        if path == "/api/health":
            self._send_json(
                HTTPStatus.OK,
                {
                    "status": "ok",
                    "modelProvider": os.environ.get("AGENT_MODEL_PROVIDER", "mock"),
                },
            )
            return
        if path == "/api/mock-government/citizen":
            self._send_json(HTTPStatus.OK, GOVERNMENT_DATA.fixture_view())
            return
        if path.startswith("/api/sessions/"):
            session_id = path.rsplit("/", 1)[-1]
            self._handle(lambda: AGENT.get(session_id))
            return
        self._send_json(HTTPStatus.NOT_FOUND, {"error": "Not found"})

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path == "/api/login":
            body = self._read_json()
            self._handle(
                lambda: AGENT.login(
                    str(body.get("username", "")),
                    str(body.get("password", "")),
                ),
                HTTPStatus.CREATED,
            )
            return
        if path == "/api/sessions":
            self._handle(AGENT.start, HTTPStatus.CREATED)
            return
        if path == "/api/chat":
            body = self._read_json()
            self._handle(
                lambda: AGENT.send(
                    session_id=body.get("sessionId", ""),
                    message=body.get("message", ""),
                    action=body.get("action"),
                    payload=body.get("payload"),
                )
            )
            return
        self._send_json(HTTPStatus.NOT_FOUND, {"error": "Not found"})

    def log_message(self, format: str, *args: Any) -> None:
        print(f"[http] {format % args}")

    def _handle(self, operation: Any, status: HTTPStatus = HTTPStatus.OK) -> None:
        try:
            self._send_json(status, operation())
        except AgentError as exc:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
        except Exception as exc:
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if not length:
            return {}
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def _send_json(self, status: HTTPStatus, payload: Any) -> None:
        self._send_bytes(
            status,
            json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            "application/json; charset=utf-8",
        )

    def _send_bytes(self, status: HTTPStatus, payload: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def main() -> None:
    host = os.environ.get("AGENT_HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", os.environ.get("AGENT_PORT", "8080")))
    print(f"Citizen Agent prototype running at http://{host}:{port}")
    print(f"Model provider: {os.environ.get('AGENT_MODEL_PROVIDER', 'mock')}")
    ThreadingHTTPServer((host, port), Handler).serve_forever()


if __name__ == "__main__":
    main()
