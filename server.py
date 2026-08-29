from __future__ import annotations

import json
import os
import threading
import urllib.error
import urllib.request
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlparse


ROOT = Path(__file__).resolve().parent
HTML_PATH = ROOT / "citizen-agent-prototype.html"
SERVICES_PATH = ROOT / "data" / "services.json"
MOCK_CITIZEN_PATH = ROOT / "data" / "citizen-newborn-mock.json"


class AgentError(Exception):
    pass


class ModelAdapter(Protocol):
    def interpret(self, message: str, context: dict[str, Any]) -> dict[str, Any]:
        """Turn a citizen utterance into one constrained workflow action."""


class GovernmentDataAdapter(Protocol):
    def collect_evidence(
        self, subject_id: str, evidence_definitions: list[dict[str, str]]
    ) -> dict[str, dict[str, Any]]:
        """Return the minimum government evidence required by one service."""

    def registered_bank_accounts(self, subject_id: str) -> list[dict[str, Any]]:
        """Return previously verified payment accounts."""


class MockGovernmentDataAdapter:
    def __init__(self, fixture: dict[str, Any]) -> None:
        self._fixture = fixture

    def collect_evidence(
        self, subject_id: str, evidence_definitions: list[dict[str, str]]
    ) -> dict[str, dict[str, Any]]:
        self._require_subject(subject_id)
        records = self._fixture["records"]
        result: dict[str, dict[str, Any]] = {}
        for definition in evidence_definitions:
            evidence_id = definition["id"]
            if evidence_id not in records:
                raise AgentError(f"Mock government record is missing: {evidence_id}")
            record = records[evidence_id]
            result[evidence_id] = {
                "label": record["label"],
                "issuer": record["issuer"],
                "recordId": record["recordId"],
                "lastUpdated": record["lastUpdated"],
                "satisfied": record["satisfied"],
                "disclosedValue": record["disclosedValue"],
            }
        return result

    def registered_bank_accounts(self, subject_id: str) -> list[dict[str, Any]]:
        self._require_subject(subject_id)
        return [dict(item) for item in self._fixture["registeredBankAccounts"]]

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
            if any(word in text for word in ("生小孩", "新生兒", "育兒", "托育", "生育")):
                return {
                    "action": "ask_question",
                    "message": text,
                    "lifeEvent": "newborn-family",
                    "serviceIds": [item["id"] for item in context["services"]],
                    "assistantMessage": "了解，這看起來與新生兒或育兒服務有關。我會整理目前目錄中可能適用的項目。",
                }
            return {
                "action": "message",
                "assistantMessage": "我理解您需要政府協助，但目前 Demo 服務目錄只有新生兒與育兒項目。正式版本會接入醫療、病假與社會救助服務目錄。",
                "noServiceMatch": True,
            }
        if state == "awaiting_service":
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
        return {
            "action": "message",
            "assistantMessage": "案件已送出。這個 Demo 下一步可以加入案件進度查詢。",
        }


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
            "awaiting_question": ["ask_question", "message"],
            "awaiting_service": ["select_service", "message"],
            "awaiting_consent": ["grant_consent", "deny_consent", "message"],
            "eligible": ["create_draft", "message"],
            "ineligible": ["message"],
            "awaiting_account": ["select_bank_account", "add_bank_account", "message"],
            "awaiting_confirmation": ["confirm_and_submit", "message"],
            "submitted": ["message"],
        }.get(state, ["message"])

        model_context = {
            "state": state,
            "allowedActions": allowed,
            "conversation": context.get("conversation", []),
            "services": context.get("services", []),
            "selectedService": context.get("selectedService"),
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
            "awaiting_question": ["ask_question", "message"],
            "awaiting_service": ["select_service", "message"],
            "awaiting_consent": ["grant_consent", "deny_consent", "message"],
            "eligible": ["create_draft", "message"],
            "ineligible": ["message"],
            "awaiting_account": [
                "select_bank_account",
                "add_bank_account",
                "message",
            ],
            "awaiting_confirmation": ["confirm_and_submit", "message"],
            "submitted": ["message"],
        }.get(state, ["message"])
        model_context = {
            "state": state,
            "allowedActions": allowed,
            "conversation": context.get("conversation", []),
            "services": context.get("services", []),
            "selectedService": context.get("selectedService"),
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
    eligibility: str = "not_checked"
    draft: dict[str, Any] | None = None
    bank_accounts: list[dict[str, Any]] = field(default_factory=list)
    selected_bank_account_id: str | None = None
    application: dict[str, Any] | None = None
    messages: list[Message] = field(default_factory=list)
    audit: list[AuditEntry] = field(default_factory=list)


class CitizenAgent:
    """Deep workflow module used by both HTTP callers and tests."""

    def __init__(
        self,
        model: ModelAdapter,
        services: list[dict[str, Any]],
        government_data: GovernmentDataAdapter,
    ) -> None:
        self._model = model
        self._services = services
        self._government_data = government_data
        self._sessions: dict[str, Session] = {}
        self._lock = threading.Lock()

    def start(self) -> dict[str, Any]:
        session = Session(
            id=str(uuid.uuid4()),
            bank_accounts=self._government_data.registered_bank_accounts(
                "citizen-demo-001"
            ),
        )
        session.messages.append(
            Message(
                actor="agent",
                text="您好，我可以協助您找出適用服務、檢查資格並準備申請。請告訴我最近發生了什麼事。",
            )
        )
        with self._lock:
            self._sessions[session.id] = session
        return self._view(session)

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
        session.evidence = {item["id"]: "locked" for item in service["evidence"]}
        session.consent = "pending"
        session.state = "awaiting_consent"
        labels = "\n• ".join(item["label"] for item in service["evidence"])
        session.messages.extend(
            [
                Message("user", command.get("message") or f"我想先申請「{service['name']}」。"),
                Message(
                    "agent",
                    (
                        f"好的，先處理「{service['name']}」，預估補助為{service['estimatedAmount']}。\n\n"
                        f"為了檢查資格，我需要一次性確認：\n• {labels}\n\n"
                        "只會取得資格判斷需要的結果。是否同意？"
                    ),
                ),
            ]
        )
        session.audit.append(AuditEntry("民眾", "選擇政府服務", service["name"]))

    def _on_grant_consent(self, session: Session, command: dict[str, Any]) -> None:
        self._require(session, "awaiting_consent")
        session.messages.append(Message("user", command.get("message") or "我同意本次資格檢查。"))
        session.consent = "granted"
        service = session.selected_service
        session.evidence_details = self._government_data.collect_evidence(
            session.subject_id, service["evidence"]
        )
        session.evidence = {
            key: "available" for key in session.evidence_details
        }
        eligible = all(
            item["satisfied"] for item in session.evidence_details.values()
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
        session.audit.extend(
            [
                AuditEntry("民眾", "授予資料存取", f"一次性、限{service['name']}資格檢查"),
                AuditEntry(
                    "規則引擎",
                    "資格判斷",
                    f"{'符合' if eligible else '不符合'}；規則版本 2026.08",
                ),
            ]
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
        session.draft = {
            "service": service["name"],
            "evidenceCount": len(service["evidence"]),
            "paymentAccount": "尚未選擇",
            "status": "等待確認",
        }
        session.state = "awaiting_account"
        session.messages.append(
            Message("agent", "申請草稿已完成。請選擇已驗證的入帳帳戶，或新增其他帳戶。")
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
        }

    def _session(self, session_id: str) -> Session:
        try:
            return self._sessions[session_id]
        except KeyError as exc:
            raise AgentError("Unknown session") from exc

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
AGENT = CitizenAgent(create_model(), load_services(), GOVERNMENT_DATA)


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
    port = int(os.environ.get("AGENT_PORT", "8080"))
    print(f"Citizen Agent prototype running at http://{host}:{port}")
    print(f"Model provider: {os.environ.get('AGENT_MODEL_PROVIDER', 'mock')}")
    ThreadingHTTPServer((host, port), Handler).serve_forever()


if __name__ == "__main__":
    main()
