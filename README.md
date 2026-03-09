# AIY Voice Kit V1 硬體驗證與關機保護（Raspberry Pi 3B）

## 目前只保留兩個功能
1. 硬體驗證 PoC（按鈕 / LED / 麥克風 / 喇叭）
2. 長按按鈕安全關機（10 秒警告，12 秒關機）

雲端 OpenAI / STT / TTS 已從目前流程移除，避免 API 成本。

## 功能 A：本地硬體驗證 PoC
- 程式：`~/aiy-assistant/aiy_button_record_play.py`
- 啟動：`~/run-local-poc.sh`

操作流程：
1. 按一下按鈕再放開
2. LED 亮 + 提示音，開始錄音
3. 使用者說話
4. 再按一下按鈕再放開
5. LED 滅 + 提示音，停止錄音並回放

備註：
- `run-local-poc.sh` 會先暫停關機守護服務，避免同時搶 GPIO
- PoC 結束後會自動把關機守護服務啟回

## 功能 B：長按關機保護
- 程式：`~/aiy-assistant/button_shutdown_guard.py`
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

## 專案結構
- `~/aiy-assistant/`：專案程式與 README
- `~/run-local-poc.sh`：硬體驗證入口
- `~/run-shutdown-guard.sh`：關機守護入口
- `~/run_aiy_assistant.sh`：相容入口（目前導向本地 PoC）
- `~/run-wifi-recover.sh`：Wi-Fi 恢復工具

## Git 版控
`~/aiy-assistant` 已初始化為本地 git repo。

常用：
- `git -C ~/aiy-assistant status`
- `git -C ~/aiy-assistant log --oneline -n 5`
