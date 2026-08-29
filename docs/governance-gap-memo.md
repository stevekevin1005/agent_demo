# Governance Gap Memo

## 問題

MyData 解決民眾單次取得資料，數位憑證皮夾解決本人出示，但兩者尚未回答 AI Agent 在民眾不在場時如何被授權代查、代填與代送。

## 本 PoC 的信任設計

- **Principal**：每個 session 只代表一位已識別的 Mock citizen，不自動代表整個家戶。
- **Authorization**：資料用途與範圍在調閱前揭露；送件與帳戶變更需要本人再次確認。
- **Tool / Action**：模型只輸出受限結構化 action；後端狀態機決定 action 是否可執行。
- **Policy Gate**：資格由規則引擎計算；模型不能核准資格或直接送件。
- **Audit**：記錄提問、服務選擇、授權、證據取得、規則結果、帳戶與送件。
- **Revocation**：目標版本將委託做成有期限且可即時撤銷的授權憑證。

## 已知治理缺口

1. 委託目前仍是 session state，不是有簽章的可驗證憑證。
2. 後端尚未完成 expiry 與即時 revoke enforcement。
3. Government evidence 為合成資料，尚未接 MyData／數位憑證皮夾。
4. Audit Log 尚未持久化或防竄改。
5. 家戶多人資料仍需逐人授權設計。
6. 規則與主管機關責任歸屬仍需正式法規映射。

## 刻意不交給模型的決策

- 身分與憑證有效性。
- 資格條件運算。
- 資料調閱授權。
- 帳戶持有人驗證。
- 正式送件與法律效果。

## 下一步

建立 Government Agent Manifest、Citizen Delegation Credential、可撤銷狀態服務與 hash-chained Audit Bundle，並以孩子滿 2 歲前的主動轉換通知作為跨部會示範。
