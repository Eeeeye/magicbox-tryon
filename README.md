# MagicBox Try-On

MagicBox Try-On generates a virtual try-on image from a portrait and a garment photo. This independent implementation uses publicly available models and supports tops, bottoms, and dresses. A Java 21 service validates the uploads, queues the task, handles duplicate submissions, and serves the result as a PNG. A Python worker uses SegFormer to locate the clothing and CatVTON Mix to generate the try-on image.

## Quick start

On macOS or Linux, install Git, JDK 21, Python 3.12, and curl. On Apple Silicon, setup uses the full Python dependency lockfile tested on that platform. On Linux, the direct dependencies are pinned. Set `PYTHON_BIN` to select a Python executable or `JAVA_HOME` to select a JDK.

```sh
git clone https://github.com/Eeeeye/magicbox-tryon.git
cd magicbox-tryon
sh scripts/setup.sh
sh scripts/run-local.sh
```

The first setup downloads about 2.36 GB of model files and sample images from pinned revisions. It can resume interrupted downloads and checks each file's size and hash. For subsequent runs, use the last command. Inference runs offline.

When the terminal prints `Try-on ready`, open the app. In the Chinese-language interface, select a garment category, then use the button above the two upload fields to load a matching sample. You can also upload your own portrait and garment photo. Use the blue button below the fields to start the task. When it finishes, you can preview the PNG, download it, or reopen it from the task history in the same browser. [Open the local app](http://127.0.0.1:8780/)

The service accepts PNG and JPEG files, with default limits of 5 MiB and 16 megapixels per image. It stores the inputs and results on the machine running the service. The Java service and Python worker listen on loopback ports 8780 and 8781, respectively. Press Ctrl-C to stop both.

## Run on a GPU server

On a Linux GPU server, install Python 3.12, JDK 21, and an NVIDIA driver compatible with PyTorch 2.9.1. Run the following commands from the project directory. The Python command runs a small calculation on the GPU to check that the driver and PyTorch build work together. [PyTorch 2.9.1 installation options](https://pytorch.org/get-started/previous-versions/#v291)

```sh
sh scripts/setup.sh
nvidia-smi
.venv/bin/python -c 'import torch; print(torch.cuda.get_device_name(0), torch.ones(1, device="cuda").item())'
TRYON_DEVICE=cuda sh scripts/run-local.sh
```

The two services exchange images over HTTP and bind to the server's loopback interface. To open the app from your computer, replace `gpu-host` with your SSH host and run `ssh -N -L 8782:127.0.0.1:8780 gpu-host` on your computer. [Open the forwarded app](http://127.0.0.1:8782/)

In a two-machine deployment, the Java host reaches the inference worker through an SSH tunnel.

1. On the GPU host, start the worker with `TRYON_DEVICE=cuda HF_HUB_OFFLINE=1 .venv/bin/python -m uvicorn inference.server:app --host 127.0.0.1 --port 8781`.
2. On the Java host, forward the worker port with `ssh -N -L 18781:127.0.0.1:8781 gpu-host`. Leave that tunnel open.
3. In a second terminal on the Java host, run `TRYON_MODE=catvton PORT=8780 TRYON_INFERENCE_URL=http://127.0.0.1:18781/infer/images sh scripts/run-java.sh`.

The end-to-end runs recorded in this repository used Apple M5 hardware with the PyTorch MPS backend. The CUDA commands above provide a way to repeat the checks on an NVIDIA GPU.

## How inference works

1. The worker center-crops the portrait to the target dimensions. For a garment photographed against a uniform background, it trims the margins, resizes the garment proportionally, and pads the image to fit.
2. SegFormer B2 identifies clothing, limbs, and the face. The worker builds a mask for the requested garment category and protects the hands when the arms hang down. For tops and bottoms, it crops around the editable region before inference. For short bottoms, it preserves the exposed lower legs.
3. CatVTON stacks the masked portrait and garment latents along the height axis. A Stable Diffusion 1.5 Inpainting U-Net denoises them using the CatVTON authors' published self-attention weights. The VAE decodes the portrait region to RGB, and the worker blends it back into the original portrait along the mask boundary.

The default output is 384 × 512 pixels with 25 DDIM steps, a guidance scale of 2.5, and seed 42. On MPS, the U-Net uses FP16 and the VAE uses FP32. This implementation uses SegFormer for automatic masking on Mac. The original CatVTON demo uses DensePose and SCHP. The asset manifest lists the revision, path, and hash for each model file and sample. [Asset manifest](inference/assets.lock.json) · [CatVTON model card](https://huggingface.co/zhengchong/CatVTON) · [CatVTON paper](https://arxiv.org/abs/2407.15886) · [Clothing segmentation model](https://huggingface.co/mattmdjaga/segformer_b2_clothes)

### Configuration

| Setting | Default | Purpose |
| --- | --- | --- |
| `PORT` / `MODEL_PORT` | 8780 / 8781 | Java and Python ports when started with `run-local.sh` |
| `TRYON_DEVICE` | auto | Selects the first available backend in the order CUDA, MPS, CPU |
| `TRYON_WIDTH` / `TRYON_HEIGHT` | 384 / 512 | Output dimensions in pixels. Each must be a multiple of 64 from 128 to 1024 |
| `TRYON_STEPS` / `TRYON_SEED` / `TRYON_GUIDANCE` | 25 / 42 / 2.5 | DDIM steps, random seed, and guidance scale |
| `QUEUE_CAPACITY` / `INFERENCE_TIMEOUT_MS` | 4 / 600000 | Waiting slots and timeout for one inference call, in milliseconds |
| `DATA_DIR` / `MAX_TASK_HISTORY` | data/tryon / 1000 | Task directory for one Java instance and maximum number of retained tasks |
| `TASK_RETENTION_SECONDS` | 604800 (7 days) | How long to keep inputs, results, and the idempotency key after a task succeeds or fails |

## Task API and failure handling

```sh
curl -fsS http://127.0.0.1:8780/api/tryon/tasks \
  -H 'Idempotency-Key: sample-upper-001' \
  -H 'X-Garment-Category: upper' \
  -F 'person=@samples/person.jpg;type=image/jpeg' \
  -F 'garment=@samples/garment.jpg;type=image/jpeg'
```

- **Submit and poll.** An accepted submission returns HTTP 202, a task ID, and a `Location` header. Poll `GET /api/tryon/tasks/{id}` for the status, timestamps, input hashes, and any error code. Once the status is `SUCCEEDED`, follow the `result` URL to download the PNG.
- **Retry a submission.** `X-Garment-Category` accepts `upper`, `lower`, or `dress`. While a task is retained, submitting the same images and category with the same idempotency key returns the original task, even after a restart. Reusing the key with different inputs returns HTTP 409.
- **Validate inputs and results.** Java decodes both images and enforces byte and pixel limits before sending a multipart request with the task ID. Before publishing a result, it checks the response's task ID, both input SHA-256 hashes, the media type, the response size, and whether the PNG decodes.
- **Handle overload and inference failures.** Each model worker processes one task at a time. A full queue returns HTTP 503 with `Retry-After`. If an accepted task times out, loses its connection to the worker, or the worker cannot locate the requested garment category, the task records the failure.
- **Recover after a restart.** The service commits the result image atomically, then commits the task status atomically. On startup, it restores completed tasks and reclaims incomplete uploads. If an interrupted task already has a committed result, it recovers as successful. Other interrupted tasks are marked `SERVER_RESTARTED`. A lock grants one Java instance exclusive use of the data directory.
- **Expire completed tasks.** Retention starts when a task succeeds or fails. Queued and running tasks stay available. After expiry, task and result requests return HTTP 404, and the key can be used for a new submission. Cleanup atomically moves the task directory before deleting its files and resumes after a restart. At the history limit, the service reclaims expired slots before returning HTTP 503 if it is still full.
- **Check health.** `GET /health/live` reports the Java process status, queue depth, and task counts. `GET /health/ready` also checks whether the inference worker is ready.

Follow a request through the Java task service, Python inference pipeline, and task repository. The API regression tests exercise concurrent retries, interrupted tasks, and expiry. [Java task service](src/tryon/TryOnService.java) · [Python inference pipeline](inference/engine.py) · [Task storage and recovery](src/tryon/TaskRepository.java) · [API regression tests](tests/result_contract.py)

## Verification

### CPU regression tests

With JDK 21, Node.js 22, and Python 3.12 installed, run the following commands. If you have already run the full setup, start with the last line.

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install -c inference/requirements.txt numpy Pillow
sh scripts/test.sh
```

GitHub Actions runs the same suite on a clean Linux runner. A controlled inference worker lets the tests reproduce queue saturation, timeouts, and malformed PNG responses. The suite also covers upload validation, concurrent requests with the same idempotency key, restart recovery, expiry, browser history, and mask behavior. A separate check verifies the five saved model outputs against the hashes in their run records. [CI workflow and runs](https://github.com/Eeeeye/magicbox-tryon/actions/workflows/test.yml)

### Browser tests and recorded runs

The browser tests use Playwright 1.62.1 and Chromium. To install them in a fresh checkout, run the commands below. You can also set `CHROME_BIN` to an existing Chrome executable or `NODE_PATH` to the `node_modules` directory of an existing Playwright installation.

```sh
npm install --prefix .build/browser playwright@1.62.1
.build/browser/node_modules/.bin/playwright install chromium
export NODE_PATH="$PWD/.build/browser/node_modules"
```

With the app running, exercise each garment category and check the saved images.

```sh
node tests/browser-e2e.cjs
TRYON_TEST_CATEGORY=lower EVIDENCE_DIR=evidence/lower node tests/browser-e2e.cjs
TRYON_TEST_CATEGORY=dress EVIDENCE_DIR=evidence/dress node tests/browser-e2e.cjs
.venv/bin/python tests/visual_acceptance.py
```

The browser test submits a task, downloads the PNG, and compares it byte for byte with the API response. It then reloads the page and reopens the result from history. For the three fixed samples, the pixel checks also verify the expected color change in the garment region and an exact match to the original background in the leftmost 5% of the frame. [Pixel checks](evidence/visual-checks.json)

The following timings are from individual runs on an Apple M5 with MPS. Each measures the time from the start of task execution in Java until the result is saved. For long attention sequences on MPS, the worker slices attention by head to limit peak memory use.

| Sample | Output size | DDIM steps | Guidance scale | Task time | Run record |
| --- | --- | --- | --- | --- | --- |
| Top | 384 × 512 | 25 | 2.5 | 41.6 s | [Top](evidence/acceptance.json) |
| Bottom | 384 × 512 | 25 | 2.5 | 37.7 s | [Bottom](evidence/lower/acceptance.json) |
| Dress | 384 × 512 | 25 | 2.5 | 44.3 s | [Dress](evidence/dress/acceptance.json) |
| Higher-resolution top | 576 × 768 | 25 | 3.5 | 93.18 s | [Higher-resolution top](evidence/high-resolution/acceptance.json) |

One sample exposes a limitation when the garment shape changes. Asked to replace long black trousers with a short white skirt, the model produced a valid PNG that retained the outline of the trousers. To reproduce it, load the bottom sample and replace its portrait with `samples/person.jpg`. Print placement, cuffs, and hems can also shift during generation. [Trouser-to-skirt run record](evidence/difficult-lower/acceptance.json)

## Models and sample images

The CatVTON authors release their model under CC BY-NC-SA 4.0. The top, dress, and accompanying portrait come from the public CatVTON demo. The skirt and its paired portrait come from the public FASHN demo. Model files and sample images are subject to their respective source licenses. [CatVTON license](https://huggingface.co/zhengchong/CatVTON#license) · [CatVTON demo](https://huggingface.co/spaces/zhengchong/CatVTON) · [FASHN sample source](https://huggingface.co/spaces/fashn-ai/fashn-vton-1.5/blob/c433f1268bfa86502f6582ab03494d3e4d7aa8bf/app.py)
