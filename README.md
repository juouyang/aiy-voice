# AIY Voice Kit V1 Echo 與關機保護（Raspberry Pi 3B）

## 目前功能
1. 功能 A：單一按鈕的本機錄音 Echo
2. 功能 B：長按按鈕安全關機（10 秒警告，12 秒關機）

雲端 OpenAI / STT / TTS 尚未接入；功能 A 是後續語音助理使用者體驗的第一個完整步驟。

## 功能 A：錄音 Echo

- 程式：`~/aiy-voice/aiy_button_echo.py`
- 啟動：`~/aiy-voice/run-echo.sh`

只使用同一顆 AIY 按鈕，短按定義為按下後在 1.2 秒內放開：

| 狀態 | 使用者動作 | 裝置回饋 | 結果 |
| --- | --- | --- | --- |
| 待命 | — | LED 熄滅 | 等待第一次短按 |
| 開始錄音 | 第一次短按 | LED 常亮、上行提示音 | 開始收音 |
| 錄音中 | 說話 | LED 持續常亮 | 持續錄音 |
| 停止並 Echo | 第二次短按 | 下行提示音，LED 閃爍 | 停止錄音並回放剛才的內容 |
| 回到待命 | 回放結束 | LED 熄滅 | 可開始下一輪 |

備註：

- `run-echo.sh` 會暫停關機守護服務，避免兩個程序同時取得 GPIO；結束 Echo 程式後會自動重新啟用守護服務。
- 因此 Echo 程式運行期間，長按不會觸發關機；請以 `Ctrl-C` 結束 Echo，回到待命的關機保護模式。

## 功能 B：長按關機保護
- 程式：`~/aiy-voice/button_shutdown_guard.py`
- 手動啟動：`~/run-shutdown-guard.sh`

長按行為：
- 按住 10 秒：警告提示音（放開可取消）
- 持續按到 12 秒：執行 `sudo /sbin/shutdown -h now`

## 自動啟動（開機後生效）
已建立 systemd 服務：`aiy-shutdown-guard.service`

常用指令：
- `sudo systemctl status aiy-shutdown-guard.service`
- `sudo systemctl restart aiy-shutdown-guard.service`
- `sudo systemctl disable --now aiy-shutdown-guard.service`

## 本機設定與安全
- 此 repository 不包含雲端 API key、帳號密碼、私有 URL 或裝置專屬設定。
- `.env`、`*.env`、`*.local`、私鑰與音量設定檔都必須只留在裝置本機，不可提交。
- Uptime Kuma heartbeat 是裝置維運用途，刻意存放在 `~/scripts/uptime-kuma/`，不屬於本專案。
- 若未來需要設定範例，請建立不含真實值的 `*.example` 檔案。

## 專案結構
- `~/aiy-voice/`：專案程式與 README
- `~/aiy-voice/run-echo.sh`：錄音 Echo 入口
- `~/run-shutdown-guard.sh`：關機守護入口
- `~/aiy-voice/volume.sh`：調整播放/麥克風增益
- `~/run-wifi-recover.sh`：Wi-Fi 恢復工具

## License

Released under the [MIT License](LICENSE).
