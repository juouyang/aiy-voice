# AIY Voice Kit V1 單一按鈕 daemon（Raspberry Pi 3B）

## 目前功能
單一 `aiy-button-daemon.service` 是唯一常駐且持有 GPIO 的程序，提供：

1. 功能 A：兩次短按的網路語音確認
2. 功能 B：長按安全關機（10 秒警告，12 秒關機）
3. 輔助手勢：為後續新功能預留的中按＋短按事件

雲端 AI agent 尚未接入。功能 A 使用 WireGuard 連到 Mac mini 的 ASR/TTS API，是後續語音助理使用者體驗的第一個完整步驟。

## 常駐按鈕行為

短按定義為按下後在 1.2 秒內放開：

| 狀態 | 使用者動作 | 裝置回饋 | 結果 |
| --- | --- | --- | --- |
| 待命 | — | LED 熄滅 | 等待第一次短按 |
| 開始錄音 | 第一次短按 | LED 常亮、上行提示音 | 開始收音 |
| 錄音中 | 說話 | LED 持續常亮 | 持續錄音 |
| 停止並確認 | 第二次短按 | 下行提示音、LED 閃爍 | 立即播放原始錄音；背景呼叫 Mac ASR，再呼叫 Mac TTS |
| 語音確認 | 原始錄音播放完 | LED 持續閃爍 | 播放 Mac TTS 的「你剛剛說：……」確認語音 |
| 回到待命 | TTS 播放結束 | LED 熄滅 | 可開始下一輪 |
| 輔助手勢待確認 | 待命時按住約 1.5–3 秒後放開 | LED 雙閃、特殊提示音 | 進入一秒確認窗口 |
| 輔助手勢觸發 | 在確認窗口內短按一次 | 確認提示音 | 目前只確認事件，預留給下一個功能 |
| 安全關機 | 任一狀態長按 10 秒 | 警告提示音 | 放開可取消；持續按到 12 秒即關機 |

長按在錄音、原始錄音播放、網路處理、TTS 回放或輔助手勢等待中具有優先權：daemon 會中止目前工作、進入關機警告。這個設計讓多種功能共用一顆按鈕，但從不由兩個程序同時操作 GPIO。

若 Mac mini 不可達、API 驗證失敗或逾時，原始錄音仍會播完；daemon 隨後播放失敗提示音並回到待命。錄音、處理音檔與 TTS 回應僅存於 `/tmp`，在完成或失敗後清除。

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
- 若未來需要設定範例，請建立不含真實值的 `*.example` 檔案。

## 專案結構
- `~/aiy-voice/`：專案程式與 README
- `~/aiy-voice/aiy_button_daemon.py`：合併功能 A/B 的常駐 daemon
- `~/run-button-daemon.sh`：systemd 使用的 daemon 啟動器
- `~/run-echo.sh`：本機錄音 Echo 的手動硬體測試入口
- `~/run-shutdown-guard.sh`：功能 B 手動測試入口
- `~/aiy-voice/systemd/aiy-button-daemon.service`：唯一 systemd unit
- `~/aiy-voice/volume.sh`：調整播放/麥克風增益
- `~/run-wifi-recover.sh`：Wi-Fi 恢復工具

## License

Released under the [MIT License](LICENSE).
