# 政好 Demo Day 簡報覆核 v2

## 結論

簡報應以「真實民眾權益痛點 → 單一 Agent 入口 → 六項信任控制 → 可重現 Demo → 治理揭露」作為五分鐘主線。評審比重最高的是產業應用場景契合度，因此必須先講具體期限與權益影響，再講技術。

## 評分對應

| 評分項目 | 比重 | 簡報證據 |
|---|---:|---|
| 產業應用場景契合度 | 35% | 民眾不知道服務、跨機關重複證明、幼兒送托 15 日申請期限 |
| 可信技術導入可行性 | 25% | Principal、Authorization、Tool/Action、Policy Gate、Audit、Revocation |
| 簡報與 Demo 呈現 | 25% | 五分鐘故事線、預錄操作影片、互動式政策與授權說明 |
| 問題洞察與創意 | 15% | 不打通所有資料庫，以逐案授權與最小資料建立跨機關代理層 |

來源：`比賽規範說明簡報.pptx.txt` 第 7 頁。

## 主辦方要求

- Demo Day 為五分鐘簡報與兩分鐘問答，超時內容不計分。
- 作品操作應以預錄影片呈現；互動式 HTML 可用於翻頁與概念說明，但不應用現場實機操作取代 Demo 影片。
- 評審不會預先閱讀文件，關鍵證據必須直接出現在現場簡報。
- 使用開源、AI 協作與合成資料的情況應在簡報或 README 揭露。
- 政府服務題必要成果包含 GitHub repository、簡報、Demo 連結或影片、Governance Gap Memo 與 README。

來源：

- `比賽規範說明簡報.pptx.txt` 第 4–10 頁。
- `痛點4_政府服務_Jim Lin_20260822.txt` 第 10 頁。

## 政府服務題的六個必答問題

1. **Principal**：Agent 代表哪一位民眾或哪一個家戶。
2. **Authorization**：代查、填表、建草稿與正式送件是否分層授權。
3. **Tool / Action**：Agent 能呼叫哪些機關與哪些動作。
4. **Policy Gate**：哪些高風險動作必須本人確認。
5. **Audit Log**：能否還原 Agent 使用的資料、規則與送件內容。
6. **Expiry / Revocation**：授權失效或撤銷後是否停止。

來源：`痛點4_政府服務_Jim Lin_20260822.txt` 第 8 頁；`0822_產業工作坊_場片.txt` 第 27 頁。

## 主張分級

### Demo 可直接證明

- AI 僅將自然語言映射為受限工作流程動作；資格與送件條件由後端狀態機控制。
- 憑證缺少、過期或撤銷時，流程停止並要求重新取得或授權。
- 政府資料與民眾自行上傳文件分開處理。
- 申請案件保存操作與授權時間線。
- 所有展示資料均為合成資料。

來源：`server.py` 的 `CitizenAgent`、`FileUserProfileStore`、憑證撤銷與送件流程；`data/services.json`；`data/citizen-newborn-mock.json`。

### 必須標示為目標設計

- 具簽章與一次性核銷能力的 Agent Delegation Credential。
- `AUDIENCE_MISMATCH`、`DATA_SCOPE_VIOLATION`、`REPLAY_DENIED` 三項機關端 Policy Gate。
- 含 `Decision`、`Trace ID` 與完整證據快照的 Audit Bundle。
- 真實 MyData、數位憑證皮夾與政府申辦 API 串接。

來源：目前程式使用 `MockGovernmentDataAdapter`，尚無正式簽章驗證、一次性 grant 核銷或 MyData API。

## 建議投影片順序

1. 政好：一句話價值主張與團隊。
2. 真實痛點：15 日期限與跨機關重複證明。
3. 架構：民眾只面對 Agent，不集中搬移所有資料。
4. Agent 六步流程。
5. 四個信任原則。
6. 三類憑證與一張收據。
7. 憑證到資格規則的自動比對。
8. 代查不等於代送。
9. 已落地與目標設計的 Policy Gate。
10. 案件級 Audit Log。
11. 預錄 Demo。
12. 價值主張與治理揭露。
13. Q&A。

## 五分鐘配置

| 範圍 | 建議時間 |
|---|---:|
| 痛點與架構 | 50 秒 |
| 流程與信任原則 | 55 秒 |
| 憑證、授權與 Policy Gate | 85 秒 |
| Audit Log | 20 秒 |
| Demo 影片 | 75–90 秒 |
| 收尾 | 15 秒 |

## 交件前檢查

- 確認主辦上傳系統是否接受 HTML；必要時同步輸出 PDF 或整份播放影片。
- Demo 影片需包含：登入、提出需求、取得憑證、限縮授權、資格判定、本人確認、送件、案件 Audit Log、撤銷後被拒絕。
- 使用非開發機與手機網路測試 `https://agentdemo.zeabur.app/`。
- 確認 QR Code、影片與所有字型在離線主辦電腦可正常顯示。
- 將檔名調整為主辦方要求的「隊伍名稱_DEMO」。
