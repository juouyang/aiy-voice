# AIY Voice Kit V1 單一按鈕 daemon（Raspberry Pi 3B）

## 目前功能
單一 `aiy-button-daemon.service` 是唯一常駐且持有 GPIO 的程序，提供：

1. 功能 A：兩次短按的網路語音確認
2. 功能 B：長按安全關機（10 秒警告，12 秒關機）
3. 輔助手勢：切換所有 daemon 聲音的三段輸出音量

功能 A 透過 WireGuard 使用 Mac mini 的 ASR/TTS API；ASR 文字會以單次、無對話歷史的 OpenAI Responses API 請求產生回覆，再交由 Mac TTS 播放。

## 常駐按鈕行為

短按定義為按下後在 1.2 秒內放開：

| 狀態 | 使用者動作 | 裝置回饋 | 結果 |
| --- | --- | --- | --- |
| 待命 | — | LED 熄滅 | 等待第一次短按 |
| 開始錄音 | 第一次短按 | LED 常亮、上行提示音 | 開始收音 |
| 錄音中 | 說話 | LED 持續常亮 | 持續錄音 |
| 停止並確認 | 第二次短按 | 下行提示音、LED 閃爍 | 立即播放原始錄音；背景呼叫 Mac ASR、OpenAI 與 Mac TTS |
| 錄音時間上限 | 錄音達 45 秒 | 下行提示音、LED 閃爍 | 自動停止錄音，接續原始錄音 Echo、ASR、OpenAI 與 TTS |
| 語音確認 | 原始錄音播放完 | LED 持續閃爍 | 播放 Mac TTS 產生的簡短 AI 回覆 |
| 取消語音本輪 | Echo 回放、網路處理或最終 TTS WAV 播放中短按一次 | 立即停止目前聲音、取消提示音、LED 熄滅 | 捨棄本輪後續回覆並回待命；下一次短按才開始新錄音 |
| 回到待命 | TTS 播放結束 | LED 熄滅 | 可開始下一輪 |
| 輔助手勢準備 | 待命時按住滿約 1.5 秒 | LED 常亮、以目前音量播放 TTS 提示「現在可放開」 | 聽到後放開；未進入 10 秒關機警告前都有效 |
| 輔助手勢待確認 | 放開後、語音提示播放完成 | LED 雙閃、特殊提示音 | 進入一秒確認窗口 |
| 切換輸出音量 | 在確認窗口內短按一次 | 切至下一段音量，播放對應的「目前音量為……」 | 安靜 → 標準 → 大聲 → 安靜 |
| 安全關機 | 任一狀態長按 10 秒 | 警告提示音 | 放開可取消；持續按到 12 秒即關機 |

長按在錄音、原始錄音播放、網路處理、TTS 回放或輔助手勢等待中具有優先權：daemon 會中止目前工作、進入關機警告。這個設計讓多種功能共用一顆按鈕，但從不由兩個程序同時操作 GPIO。

若在「現在可放開」的句尾就已按下確認短按，daemon 也會記住該次操作，並在提示音完成後切換音量；不需要精準抓住 LED 雙閃才按。

若 Mac mini 不可達、API 驗證失敗或逾時，原始錄音仍會播完；daemon 隨後播放失敗提示音並回到待命。若 OpenAI 未設定或暫時失敗，則維持原有的「你剛剛說：……」ASR 確認語音。錄音、處理音檔與 TTS 回應僅存於 `/tmp`，在完成或失敗後清除。

## 唯一 systemd 服務

開機後啟動的唯一服務是 `aiy-button-daemon.service`。

常用指令：

- `sudo systemctl status aiy-button-daemon.service`
- `sudo systemctl restart aiy-button-daemon.service`
- `sudo systemctl disable --now aiy-button-daemon.service`

## 手動單獨測試

以下啟動器會先暫停 `aiy-button-daemon.service`，離開程式後再恢復它；因此手動測試期間同樣只有一個程序取得 GPIO。

- 功能 A Echo：`~/run-echo.sh`
- 功能 B 長按關機：`~/run-shutdown-guard.sh`

測試功能 B 時，10 秒會播放警告音；若只想確認警告，請在 12 秒前放開按鈕以取消關機。

## 本機設定與安全
- 此 repository 不包含雲端 API key、帳號密碼、私有 URL 或裝置專屬設定。
- `.env`、`*.env`、`*.local`、私鑰與音量設定檔都必須只留在裝置本機，不可提交。
- daemon 可從使用者私有的 `~/.config/aiy-voice/omlx.env` 載入 `OMLX_BASE_URL` 與 `OMLX_API_KEY`；該檔案應為 `600`，且不可提交。
- 若存在 `~/.config/aiy-voice/openai.env`，daemon 會讀取 `OPENAI_API_KEY` 與可選的 `OPENAI_MODEL`（預設 `gpt-5.4-mini`）。每次只傳送 ASR 文字，不傳送 WAV；請求設為 `store: false`、不保留對話歷史、`reasoning: none`，輸入最多 600 個字元，並硬性限制輸出最多 120 tokens 與 120 個字元。實際每輪 input/output token 用量只寫入本機 service log，不含內容。
- 若存在 `~/.config/aiy-voice/ntfy.env`，daemon 會使用其中的 `NTFY_BASE_URL` 與 `NTFY_TOPIC`，在 ASR 成功後背景傳送辨識文字。通知不使用 token，失敗只記錄 log，絕不延遲或中斷 Echo／TTS。
- 輔助手勢的預先生成 TTS 提示音及音量公告存放於 `assets/gain/{quiet,normal,loud}/`，隨專案版本追蹤。各目錄的固定 gain 分別為安靜 0.35×、標準 0.65×、大聲 1.00×，因此裝置播放時不必重新計算。
- 選取的輸出音量只寫入裝置本機的 `~/.config/aiy-voice/output-volume.env`，並在 daemon 重啟後保留；首次安裝預設為大聲，與原先已驗證的音量相同。
- 音量 profile 套用於固定提示音、錄音 Echo 回放、Mac TTS 回應、一般 beep 與關機提示。動態 WAV 會在 `/tmp` 建立一次縮放版本並在播放後清除。
- 音量 profile 只控制輸出端，永遠不改變錄音輸入的 `AIY_MIC_GAIN`。
- 錄音預設在 45 秒自動停止，可用裝置本機環境變數 `AIY_MAX_RECORDING_SEC` 調整；這可避免原始 WAV 超過 ASR 上傳上限。
- 若未來需要設定範例，請建立不含真實值的 `*.example` 檔案。

## 專案結構
- `~/aiy-voice/`：專案程式與 README
- `~/aiy-voice/aiy_button_daemon.py`：合併功能 A/B 的常駐 daemon
- `~/aiy-voice/run-button-daemon.sh`：systemd 使用的 daemon 啟動器
- `~/run-echo.sh`：本機錄音 Echo 的手動硬體測試入口
- `~/run-shutdown-guard.sh`：功能 B 手動測試入口
- `~/aiy-voice/systemd/aiy-button-daemon.service`：唯一 systemd unit
- `~/aiy-voice/volume.sh`：調整播放/麥克風增益
- `~/run-wifi-recover.sh`：Wi-Fi 恢復工具

## License

Released under the [MIT License](LICENSE).
