# AIY Voice Kit V1 單一按鈕 daemon（Raspberry Pi 3B）

## 目前功能
單一 `aiy-button-daemon.service` 是唯一常駐且持有 GPIO 的程序，提供：

1. 功能 A：兩次短按的網路語音確認
2. 功能 B：長按安全關機（10 秒警告，12 秒關機）
3. 輔助手勢：切換所有 daemon 聲音的三段輸出音量

功能 A 透過 WireGuard 使用 Mac mini 的 ASR/TTS API；ASR 文字交由內網 T450 的 OpenCode HTTP server 產生回覆，並以短期對話、固定家庭背景與即時本機時間補足上下文，再交由 Mac TTS 播放。

## 常駐按鈕行為

短按定義為按下後在 1.2 秒內放開：

| 狀態 | 使用者動作 | 裝置回饋 | 結果 |
| --- | --- | --- | --- |
| 待命 | — | LED 熄滅 | 等待第一次短按 |
| 開始錄音 | 第一次短按 | LED 常亮、上行提示音 | 開始收音 |
| 錄音中 | 說話 | LED 持續常亮 | 持續錄音 |
| 停止並確認 | 第二次短按 | 下行提示音、LED 閃爍 | 立即播放原始錄音；背景呼叫 Mac ASR、OpenCode 與 Mac TTS |
| 錄音時間上限 | 錄音達 45 秒 | 下行提示音、LED 閃爍 | 自動停止錄音，接續原始錄音 Echo、ASR、OpenCode 與 TTS |
| 語音確認 | 原始錄音播放完 | LED 持續閃爍 | 播放 Mac TTS 產生的簡短 AI 回覆 |
| 本機短期記憶 | AI 回覆完整播放完畢 | 無額外聲光提示 | 記住本輪問答，供接下來 3 分鐘內的後續對話理解上下文 |
| 固定家庭背景與時間 | 每個 OpenCode session／每次提問 | 無額外聲光提示 | 載入管理者設定的家庭背景，並附入 Asia/Taipei 即時時間；不會自行寫入新資料 |
| 取消語音本輪 | Echo 回放、網路處理或最終 TTS WAV 播放中短按一次 | 立即停止目前聲音、取消提示音、LED 熄滅 | 捨棄本輪後續回覆並回待命；下一次短按才開始新錄音 |
| 回到待命 | TTS 播放結束 | LED 熄滅 | 可開始下一輪 |
| 輔助手勢準備 | 待命時按住滿約 1.5 秒 | LED 常亮、以目前音量播放 TTS 提示「現在可放開」 | 聽到後放開；未進入 10 秒關機警告前都有效 |
| 輔助手勢待確認 | 放開後、語音提示播放完成 | LED 雙閃、特殊提示音 | 進入一秒確認窗口 |
| 切換輸出音量 | 在確認窗口內短按一次 | 切至下一段音量，播放對應的「目前音量為……」 | 安靜 → 標準 → 大聲 → 安靜 |
| 安全關機 | 任一狀態長按 10 秒 | 警告提示音 | 放開可取消；持續按到 12 秒即關機 |

長按在錄音、原始錄音播放、網路處理、TTS 回放或輔助手勢等待中具有優先權：daemon 會中止目前工作、進入關機警告。這個設計讓多種功能共用一顆按鈕，但從不由兩個程序同時操作 GPIO。

若在「現在可放開」的句尾就已按下確認短按，daemon 也會記住該次操作，並在提示音完成後切換音量；不需要精準抓住 LED 雙閃才按。

若 Mac mini 不可達、API 驗證失敗或逾時，原始錄音仍會播完；daemon 隨後播放失敗提示音並回到待命。若 OpenCode server 未啟動、OAuth 未登入或暫時失敗，則維持原有的「你剛剛說：……」ASR 確認語音。錄音、處理音檔與 TTS 回應僅存於 `/tmp`，在完成或失敗後清除。

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

## OpenCode agent

T450 上常駐的 OpenCode HTTP server 使用自己的 OpenAI OAuth；AIY daemon 只透過內網 HTTP Basic Auth 呼叫它，預設模型為 `openai/gpt-5.6-luna-fast`。這取代 daemon 直接呼叫 OpenAI API 與 Raspberry Pi 本機 Pi RPC 的路徑；Mac mini 仍只負責 ASR/TTS，AIY 也不需要 OpenAI API key 或 OAuth credential。

daemon 在第一次短按、開始錄音時背景建立一個遠端 OpenCode session，避免 Echo 結束後才承擔建立 session 的等待。最終 TTS 完整播放後才保留這一輪對話；閒置 3 分鐘、按鈕取消、關機流程、agent 失敗或 TTS 無法播放時，都會 abort 並刪除 server-side session。`AIY_MEMORY_MAX_TURNS` 保留為相容參數，但預設 `0` 表示不限制輪數；三分鐘閒置才是新 session 的正式邊界。

daemon 只在私有 `~/.local/state/aiy-voice/opencode-sessions.json` 保存尚待清理的 opaque session ID，權限為 `600`，從不寫入對話內容或家庭背景。service 重啟後會先刪除其中殘留的 server session，再開始新的語音對話。

每次 HTTP prompt 都明確關閉 OpenCode 所有內建工具：bash、讀寫檔案、glob、grep、task、skills、webfetch、websearch 等。因此目前 agent 沒有網路查詢或裝置控制能力；它只負責理解對話並產生短文字回覆。這層限制不會取代 OpenCode server HTTP API 的網路邊界，server 仍應只繫結在受信任的私有 LAN 並啟用密碼。

## Pi RPC 手動 baseline

`pi_rpc_baseline.py` 是不接 GPIO 的手動效能驗證工具。每次執行只啟動一個常駐 Pi RPC process，循序送入問題並在結束時關閉它，方便比較首題與後續題的延遲與 RSS。輸出的 `reported-tokens` 是 Pi RPC 提供的數值；目前使用 ChatGPT Codex OAuth 時可能固定為 `0`，不可當成實際用量統計。

先完成 Pi 的 ChatGPT Codex OAuth 登入後，在 AIY 上執行：

```bash
python3 ~/aiy-voice/pi_rpc_baseline.py --repeat 3 "Hi"
```

也可傳入連續對話，例如：

```bash
python3 ~/aiy-voice/pi_rpc_baseline.py "我叫小明。" "我叫什麼？"
```

baseline 固定使用 `openai-codex/gpt-5.6-luna`、`thinking off`，並傳入 `--no-session --no-builtin-tools --no-extensions --no-skills --no-prompt-templates --no-context-files`。因此預設沒有網路、bash、讀寫檔案或其他工具；它只驗證 Python 透過 JSONL 與常駐 Pi RPC 溝通的成本。

第二階段可明確加上唯一的自製公開網頁工具：

```bash
python3 ~/aiy-voice/pi_rpc_baseline.py --web-fetch "請查目前竹北天氣；只用 web_fetch 取得公開資料，並用兩句繁體中文回答。"
```

`--web-fetch` 仍會停用所有 Pi built-in tools、skills、prompt templates、context files 與自動發現的 extension；只以明確路徑載入 `pi_extensions/aiy_web_fetch.ts` 的 `web_fetch`。它不使用網域白名單，但只接受公開的 HTTP(S) 位址，會拒絕 localhost、私有／保留 IP、內網 DNS 結果、含帳密 URL、非標準連接埠及所有重新導向到這些目標的請求。每次查詢總逾時 8 秒、最多 3 次重新導向、最多讀取 24 KiB 並回傳 6,000 個字元的純文字；這既限制 context 用量，也讓網頁中的提示注入內容只作不可信資料處理。它是 `web_fetch`，不是 web search：模型必須選擇可公開存取的來源網址。

## 本機設定與安全
- 此 repository 不包含雲端 API key、帳號密碼、私有 URL 或裝置專屬設定。
- `.env`、`*.env`、`*.local`、私鑰與音量設定檔都必須只留在裝置本機，不可提交。
- daemon 可從使用者私有的 `~/.config/aiy-voice/omlx.env` 載入 `OMLX_BASE_URL` 與 `OMLX_API_KEY`；該檔案應為 `600`，且不可提交。
- daemon 不再讀取 `~/.config/aiy-voice/openai.env` 或直接呼叫 OpenAI API；可保留該私有檔案供其他用途，但它不影響本服務。OpenCode server 使用自己的 ChatGPT/OpenAI OAuth；請勿把 OAuth credential 複製進 repository 或 AIY 環境檔。
- daemon 從私有 `~/.config/aiy-voice/opencode.env` 載入 T450 server URL、Basic Auth credential 與模型設定。請從 [範例](examples/opencode.env.example) 複製後填入真實值，檔案權限設為 `600`；不可提交。預設使用 `AIY_OPENCODE_MODEL=openai/gpt-5.6-luna-fast`、`AIY_OPENCODE_TIMEOUT_SEC=45`、`AIY_AGENT_MAX_INPUT_CHARS=600` 與 `AIY_AGENT_MAX_REPLY_CHARS=120`。
- `AIY_MEMORY_WINDOW_SEC` 預設為 180 秒，是 session 的正式閒置邊界。`AIY_MEMORY_MAX_TURNS=0` 預設停用舊版三輪上限；若日後需要相容行為，設為正整數即可。
- daemon 每次送 OpenCode 前，以 `AIY_ASSISTANT_TIMEZONE`（預設 `Asia/Taipei`）取得裝置目前時間，作為可信 system context；這不是模型自行推測的時間。
- 固定家庭背景放在裝置私有的 `~/.config/aiy-voice/assistant-profile.md`，每次 agent prompt 以 trusted system context 附入，最多 1,200 個字元。適合放裝置地點與共享使用情境；不可提交。請從 [範例](examples/assistant-profile.md.example) 複製後填入裝置專屬內容。這份檔案不是模型的可寫長期記憶，模型不會自行新增或修改其中資料，也不可僅根據聲音猜測目前使用者身份。
- 若存在 `~/.config/aiy-voice/ntfy.env`，daemon 會使用其中的 `NTFY_BASE_URL` 與 `NTFY_TOPIC`，在 OpenCode 回覆後背景傳送同一則「你說：ASR 文字／AI：回覆」通知。通知不使用 token，失敗只記錄 log，絕不延遲或中斷 Echo／TTS；若 agent 失敗，則只傳 ASR 文字。
- 輔助手勢的預先生成 TTS 提示音及音量公告存放於 `assets/gain/{quiet,normal,loud}/`，隨專案版本追蹤。各目錄的固定 gain 分別為安靜 0.35×、標準 0.65×、大聲 1.00×，因此裝置播放時不必重新計算。
- 選取的輸出音量只寫入裝置本機的 `~/.config/aiy-voice/output-volume.env`，並在 daemon 重啟後保留；首次安裝預設為大聲，與原先已驗證的音量相同。
- 音量 profile 套用於固定提示音、錄音 Echo 回放、Mac TTS 回應、一般 beep 與關機提示。動態 WAV 會在 `/tmp` 建立一次縮放版本並在播放後清除。
- 音量 profile 只控制輸出端，永遠不改變錄音輸入的 `AIY_MIC_GAIN`。
- 錄音預設在 45 秒自動停止，可用裝置本機環境變數 `AIY_MAX_RECORDING_SEC` 調整；這可避免原始 WAV 超過 ASR 上傳上限。
- 若未來需要設定範例，請建立不含真實值的 `*.example` 檔案。

## 專案結構
- `~/aiy-voice/`：專案程式與 README
- `~/aiy-voice/aiy_button_daemon.py`：合併功能 A/B 的常駐 daemon
- `~/aiy-voice/opencode_voice.py`：不含第三方套件的 OpenCode HTTP session client
- `~/aiy-voice/examples/opencode.env.example`：AIY 連向私有 OpenCode server 的設定範例
- `~/aiy-voice/pi_rpc_baseline.py`：不接 GPIO 的 Pi RPC 常駐效能測試
- `~/aiy-voice/pi_extensions/aiy_web_fetch.ts`：Pi 唯一可選的受控公開網頁工具
- `~/aiy-voice/pi_extensions/aiy_voice_context.ts`：Pi 手動 baseline 可用的私有固定背景載入器
- `~/aiy-voice/run-button-daemon.sh`：systemd 使用的 daemon 啟動器
- `~/run-echo.sh`：本機錄音 Echo 的手動硬體測試入口
- `~/run-shutdown-guard.sh`：功能 B 手動測試入口
- `~/aiy-voice/systemd/aiy-button-daemon.service`：唯一 systemd unit
- `~/aiy-voice/volume.sh`：調整播放/麥克風增益
- `~/run-wifi-recover.sh`：Wi-Fi 恢復工具

## License

Released under the [MIT License](LICENSE).
