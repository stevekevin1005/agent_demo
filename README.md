# Government Citizen Agent Prototype

Run the backend with no API key:

```powershell
python .\server.py
```

Then open <http://127.0.0.1:8080>.

The Demo login page displays the default credentials directly:

```text
username: user
password: password
```

Any safe username can be used with the Demo password so multiple presenters can
have separate local wallets. Each profile is stored at
`data/users/<username>.json`; passwords are not stored. These files are ignored
by Git and may be ephemeral on container hosting without a persistent volume.
New profiles start with an empty credential wallet; mock government records are
not treated as credentials until the citizen completes the issuance flow.
Production authentication should replace this Demo login with mobile identity,
a natural-person certificate, mobile natural-person certificate, or health-card
credential verification.

The default `mock` model is deterministic. To use an OpenAI-compatible model,
set environment variables instead of placing a key in source code:

```powershell
$env:AGENT_MODEL_PROVIDER = "openai-compatible"
$env:MODEL_API_KEY = "<your key>"
$env:MODEL_BASE_URL = "https://api.openai.com/v1"
$env:MODEL_NAME = "gpt-4.1-mini"
python .\server.py
```

For an Azure Foundry Responses API deployment:

```powershell
$env:AGENT_MODEL_PROVIDER = "azure-foundry-responses"
$env:MODEL_ENDPOINT = "https://<resource>.services.ai.azure.com/openai/v1/responses"
$env:MODEL_API_KEY = "<rotated key>"
$env:MODEL_NAME = "<model deployment name>"
python .\server.py
```

`MODEL_NAME` is the deployment name shown in Azure Foundry, not the endpoint
hostname. API keys should be rotated if they are pasted into chat, logs, or
source control.

This prototype also includes `start-azure.ps1`, preconfigured with the current
Foundry endpoint and `gpt-5.6-sol` deployment. After rotating the exposed key:

```powershell
$env:MODEL_API_KEY = "<new key>"
.\start-azure.ps1
```

If `MODEL_API_KEY` is not set, the script prompts for it securely without
echoing the value or writing it to disk.

For a plaintext local-only setup, copy `local-secrets.example.ps1` to
`local-secrets.ps1` and replace the placeholder. The real secrets file is
ignored by `.gitignore` and loaded automatically by `start-azure.ps1`.

The model only maps natural language to allowed workflow actions. Eligibility,
consent, account selection, and submission remain deterministic backend logic.
When the page is opened through the backend, the text box accepts freeform
citizen messages. The model receives only the current workflow state and a
limited catalog of allowed choices, then returns one structured action.

After a service is selected, the Agent separates required evidence into:

- Government credentials: the UI names the exact issuing department before
  consent. A valid credential may still be shown as unauthorized until the
  citizen grants this Agent one-time access for the selected service.
- Citizen-provided documents: records such as an involuntary-separation
  certificate, lease, or medical receipt use a file input instead of pretending
  the government already holds them.

The side panel shows the citizen's mock government credential wallet, including
usable, unauthorized, and expired credentials. Uploaded document contents are
not sent to the language model; this in-memory Demo records only file metadata
and enforces a 10 MB limit.

Mock government records are stored in
`data/citizen-newborn-mock.json`. The backend reads them through a
`GovernmentDataAdapter` and returns only the evidence required by the selected
service. `GET /api/mock-government/citizen` exposes the fixture for Demo
inspection.

Current Demo scenarios:

- Newborn and childcare: recurring childcare support, birth payment, childcare
  service subsidy.
- Medical hardship: short-term assistance for qualifying medical expenses.
- Housing: rental subsidy.
- Employment: unemployment benefit.
- Existing case lookup: review status, missing-document notice, next step, and
  estimated completion date.

## HTTP interface

- `POST /api/sessions` starts a conversation.
- `POST /api/login` validates the Demo login and starts a conversation with the
  username's persisted credential wallet.
- `POST /api/chat` sends a message or explicit workflow action.
- `GET /api/sessions/{id}` reads the complete current state.
- `GET /api/health` reports backend and model-provider status.

`POST /api/chat` request:

```json
{
  "sessionId": "...",
  "message": "我想申請育兒生活補助"
}
```

When a required government credential is absent or expired, the workflow enters
`awaiting_credentials`. The backend supplies a credential-specific application
schema instead of a generic form. For example, a medical-event credential asks
for health-card number and visit date, while a residency credential asks for
household address and registration date. The Demo validates the required
fields, deduplicates shared fields such as national ID, then submits all missing
credentials together to their simulated department APIs. It generates a
credential reference and expiry for each credential and persists them before
asking whether the citizen authorizes retrieval.
Application field values remain in the local profile and are not included in
the language-model context.

## Docker / Zeabur

The repository includes a root `Dockerfile`. Zeabur can deploy it directly.
Configure the model environment variables in the Zeabur service and let Zeabur
provide `PORT`:

```text
AGENT_MODEL_PROVIDER=azure-foundry-responses
MODEL_ENDPOINT=https://<resource>.services.ai.azure.com/openai/v1/responses
MODEL_NAME=<deployment-name>
MODEL_API_KEY=<secret>
```

The container listens on `0.0.0.0:$PORT` and exposes `/api/health`.

For deterministic UI actions:

```json
{
  "sessionId": "...",
  "action": "select_service",
  "payload": {
    "serviceId": "childcare-benefit-2026"
  }
}
```
