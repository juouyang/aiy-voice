# AIY Voice Kit V1 重建紀錄（Raspberry Pi 3B）

## 專案目標
將原本無法開機的 Raspberry Pi 3B + Google AIY Voice Kit V1 + Camera v1 從故障狀態重建為可用系統，並完成：
1. 重新安裝官方維護 OS（非舊 AIY 客製系統）
2. 驗證 AIY 核心硬體（按鈕、LED、喇叭、麥克風、相機）
3. 完成按鍵語音流程 PoC（PTT）
4. 串接雲端 LLM（含語音理解與回覆）

## 1) 故障排除與重灌
### 初始狀態
- 上電後無法正常開機
- 舊 SD 卡已損壞、無法在電腦讀取

### 排除結果
- 更換電源後可正常啟動（電源品質是關鍵）
- 使用新 SD 卡（32GB Class10）重灌

### 安裝系統
- OS：Debian / Raspberry Pi 官方維護映像（Lite, headless）
- 啟用 SSH 與基本網路設定

## 2) 系統與 SD 寫入優化
已完成低寫入設定（延長 SD 壽命）：
- journald 改為 volatile（記憶體）
- /tmp、/var/tmp、/var/log 掛載為 tmpfs
- noatime 掛載選項
- 停用高頻背景寫入 timer（apt/man-db/logrotate 等）

注意：此模式偏向耐久優先，系統不會自動更新，log 也不保留跨重開機歷史。

## 3) AIY/Camera 硬體啟用與驗證
### AIY overlay 啟用
已在 /boot/firmware/config.txt 啟用：
- dtparam=i2c_arm=on
- dtparam=i2s=on
- dtoverlay=googlevoicehat-soundcard

### 相機（Camera v1 / OV5647）
- rpicam-hello --list-cameras 可正確偵測 OV5647

### 音訊裝置
- aplay -l / arecord -l 可見 Google voiceHAT 聲卡

### 按鈕/LED GPIO
- 按鈕：GPIO23（active-low）
- LED：GPIO25（1=亮, 0=滅）

### 按鈕事件測試結果
- 實測按 5 下 -> 事件 5 falling + 5 rising，判定正常

## 4) 本地 PoC（零 API 成本）：按一下開始、再按一下停止
檔案：~/aiy-assistant/aiy_button_record_play.py
啟動：~/run-local-poc.sh

功能：
- 短按一下：提示音 + 開始錄音（LED 亮）
- 再短按一下：提示音 + 停止錄音，並回放（LED 滅）
- 本模式不呼叫外部 API，純本地硬體驗證流程

已處理：
- AIY 音效卡格式限制（S32_LE / 48000 / 2ch）
- 錄音增益與播放增益調整

### 音量工具
檔案：~/volume.sh

用法：
~/volume.sh 60

用途：
- 更新 ~/.aiy_volume.env
- 同步更新測試腳本預設 gain
- 播放 0.2 秒 beep 做音量確認

## 5) 雲端 LLM 串接（可選）
檔案：~/aiy-assistant/aiy_assistant.py
啟動器：~/run_aiy_assistant.sh

目前設計：
- PTT 錄音（按住錄、放開送出）
- 雲端語音理解 + 回答生成
- 回覆音訊播放
- 文字紀錄 log（heard/reply/raw_text）

註：雲端模式會產生 API 費用，若只做硬體驗證建議使用本地 PoC 模式。

log 位置：~/aiy-assistant/logs/assistant.log

啟動：
~/run_aiy_assistant.sh

## 6) 目前狀態（階段成果）
- 從無法開機恢復到可穩定開機
- 官方維護 OS 重裝完成
- AIY Voice Kit 硬體（按鈕/LED/喇叭/麥克風）驗證完成
- Camera v1 偵測正常
- 已能完成零 API 成本的本地錄放音 PoC
- 已串接雲端 LLM 語音回覆 Demo

## 7) 獨立關機保護（與 PoC 分離）
檔案：~/aiy-assistant/button_shutdown_guard.py
啟動：~/run-shutdown-guard.sh

行為：
- 長按 10 秒：播放警告提示音（可放開取消）
- 持續按到 12 秒：執行 `sudo /sbin/shutdown -h now`

設計原則：關機流程獨立執行，不和語音 PoC 程式耦合。

## 8) 後續建議（智能助手正式化）
目前版本屬於硬體與流程驗證 PoC，若要進入真正智能助手，建議下一步：
1. 增加工具能力（Clock / Weather / Calendar / Memory）
2. 加上更嚴格安全策略（兒童模式、工具白名單）
3. 增加錯誤復原與重試（網路/API 異常）
4. systemd 服務化（開機自啟）
5. 依需求評估編排層（自建 Orchestrator 或 OpenClaw 類框架）

## 9) 安全備註
- API Key 請勿寫入公開文件或版本控制
- 若疑似外洩請立即 revoke/rotate
- 建議將敏感設定放在 ~/.aiy_openai.env 並設權限 600
