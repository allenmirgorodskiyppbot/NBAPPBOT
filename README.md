# NBAPPBOT — Knicks Research Chat

A private web chatbot for `data/Knicks_2026_27_Completed.xlsx`. The chat app and
the Qwen3 4B model both run on your Akash deployment. No OpenAI account, API key,
external inference subscription, custom Docker build, or container registry account is needed.

The app queries Excel values using SQLite and gives the model tools to read sheets,
find phrases, and calculate aggregates. Answers should cite workbook sheets and rows.
The workbook is a snapshot through **27 October 2026**, built **28 October 2026**.
It is not a live NBA feed. Only the Excel file is included; the companion ZIP is not loaded.

## Deploy in Akash Console

1. Open [`deploy.yaml`](deploy.yaml) on GitHub and click **Raw**, then save the file.
   Alternatively, copy the entire raw YAML into Akash's SDL editor.
2. In your private copy or the Akash editor, replace
   `REPLACE_WITH_A_PRIVATE_PASSWORD_16_CHARS_OR_LONGER` with a unique password of
   at least 16 characters. Keep your real password out of GitHub.
3. At the **Import SDL** prompt, upload that file or paste its YAML. Name the
   deployment `knicks-chat`. Leave `SOURCE_REF` and the internal model URL unchanged.
4. Request provider quotes. Compare the displayed total price, region and uptime.
   This CPU starter requests **5 vCPUs, 10 GiB RAM and 23 GiB ephemeral storage**
   across two services. No GPU is required. The `pricing.amount` values are bid
   ceilings, not a monthly cost estimate.
5. Select a provider and review the total, then confirm the deployment using the
   funding flow shown in your Console. Managed Console uses account credits;
   Console Air has a wallet-based flow.
6. Open **Logs** for `chatbot`. Dependencies and the workbook download automatically,
   followed by the approximately 2.5 GB model. First startup can take several minutes.
   Look for **`KNICKS CHAT READY`**. The web page also shows startup progress.
7. Open the public URI for **`chatbot`**, enter the password you set, and ask:
   **“What is Brunson averaging in 2026–27?”** Use an HTTPS endpoint before entering
   the password; if your provider only gives HTTP, configure TLS with a custom domain
   or HTTPS reverse proxy first.

Provider selection, payment and deployment confirmation happen in your Akash account;
adding these files to GitHub does not start or bill an Akash deployment.

## What is running

| Service | Image | CPU | RAM | Root disk | Network |
| --- | --- | ---: | ---: | ---: | --- |
| `chatbot` | `python:3.12.12-slim-bookworm` | 1 | 2 GiB | 3 GiB | Public web port 80 → 8000 |
| `model` | `ollama/ollama:0.40.13` | 4 | 8 GiB | 20 GiB | Internal port 11434, accessible to chatbot |

The official Python image downloads the application from a fixed GitHub commit at startup.
The bootstrap script downloads the original workbook from a fixed commit and verifies its
SHA-256 checksum. It installs pinned direct Python dependencies and starts one web worker.
There is no need to build or publish your own Docker image before importing this SDL.

Qwen3 4B is a small starter model. CPU answers can take a few minutes, and its answers
need checking against their citations. One question is processed at a time. For faster
responses, add an NVIDIA GPU resource to the model service; larger models also need more
RAM/VRAM and a new `OLLAMA_MODEL` value. Review new provider quotes before applying changes.

Only the chat application is public. Requests for chat and workbook queries require
`CHAT_PASSWORD`; the default placeholder is rejected. The model is not publicly exposed.
Questions are passed to the model inside your deployment. Conversation history stays in
the current browser tab, with temporary server job results held in memory for up to 30 minutes.
Closing or restarting the deployment clears those temporary results.

Storage in this starter is ephemeral. A fresh container downloads the model and rebuilds
the workbook index again; workbook data stays in GitHub. This keeps provider requirements
simple. Persistent storage can be added later if repeated model downloads become an issue.

## Troubleshooting

| Symptom | What to check |
| --- | --- |
| SDL error | Import the raw YAML, not the GitHub webpage. Keep indentation and `version: "2.0"`. This template uses the current `uact` denomination. |
| No provider quotes | Remove extra region/provider filters. Check the maximum price and resource availability. |
| “Set a private CHAT_PASSWORD” | Replace the placeholder in the Akash editor, then update the deployment. |
| Model downloading | Wait and inspect `chatbot` logs for download progress. The model is approximately 2.5 GB. |
| “Startup failed” | Check both service logs. The provider needs outbound access to GitHub, PyPI and Ollama's model registry. Check disk space as well. |
| Python package not found / app restart | Check bootstrap and pip errors in `chatbot` logs. |
| Model process killed / out of memory | Increase model RAM, keep one parallel request and retain the 4B model for this starter. |
| Slow answers | CPU inference is slower. Ask a narrow question; consider a GPU provider. The UI polls jobs so long model calls do not require a long-lived ingress request. |
| Data newer than the workbook | Not available automatically. Upload an updated workbook and update the pinned reference and checksum in `chatbot/bootstrap.py`, then pin the new source commit in the SDL. |

## Local development

From the repository root, with Python 3.12 and [Ollama](https://docs.ollama.com):

```sh
python -m venv .venv
. .venv/bin/activate
pip install -r chatbot/requirements.txt
ollama pull qwen3:4b
export OLLAMA_URL=http://127.0.0.1:11434
export WORKBOOK_PATH="$PWD/data/Knicks_2026_27_Completed.xlsx"
read -rsp 'Choose a chat password (16+ characters): ' CHAT_PASSWORD
export CHAT_PASSWORD
python -m uvicorn app:app --app-dir chatbot --host 127.0.0.1 --port 8000 --no-access-log
```

Ollama must be running (`ollama serve` if the desktop/server service is not already active).
Open `http://127.0.0.1:8000` locally. Run the offline checks with:

```sh
cd chatbot
python -m unittest -v test_chatbot
```

Tests cover missing values, table boundaries, original row references, read-only SQL,
query timeouts, password protection and a simulated model/tool conversation. Live model
inference and an actual Akash provider deployment must be checked after deployment.

## References

- [Akash SDL syntax](https://akash.network/docs/developers/deployment/akash-sdl/syntax-reference/)
- [Akash Console deployment flow](https://akash.network/docs/developers/deployment/akash-console/getting-started/)
- [Ollama Docker deployment](https://docs.ollama.com/docker)
- [Qwen3 4B model](https://ollama.com/library/qwen3:4b)
