FROM python:3.11-slim

WORKDIR /srv

# Pull OS security patches available after the base image was published.
# Trivy (ignore-unfixed) fails the security workflow on fixable HIGH/CRITICAL CVEs.
RUN apt-get update \
    && apt-get upgrade -y --no-install-recommends \
    && apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-chi-sim tesseract-ocr-eng \
    && rm -rf /var/lib/apt/lists/*

# 国内构建提速：设置 ARG PIP_MIRROR=true 使用清华 pip 源
ARG PIP_MIRROR=
RUN if [ -n "$PIP_MIRROR" ]; then \
        pip config set global.index-url https://pypi.tuna.tsinghua.edu.cn/simple; \
    fi

# Patch common fixable CVEs shipped with the base image / transitive deps.
# The base ``python:3.11-slim`` image ships setuptools ~70 and may bundle
# an older msgpack.  Plain ``pip install --upgrade`` sometimes skips them
# because they are imported or pinned by pip's own internals.
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir --upgrade --force-reinstall \
        "setuptools>=78" "wheel>=0.46.2" "msgpack>=1.2.1"

COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade -r requirements.txt \
    && pip install --no-cache-dir --upgrade \
        "setuptools>=78" "wheel>=0.46.2" "msgpack>=1.2.1"

COPY . .

# 下载前端依赖到本地（走 npmmirror 国内镜像；已存在则跳过）
RUN python scripts/download_vendor.py

# HuggingFace 国内镜像，首次启动自动下载 bge 模型时用
ENV HF_ENDPOINT=https://hf-mirror.com
ENV HF_HOME=/home/app/.cache/huggingface

# Runtime has no need for root privileges.
RUN groupadd --gid 10001 app && \
    useradd --uid 10001 --gid app --create-home app && \
    mkdir -p /srv/uploads /home/app/.cache/huggingface && \
    chown -R app:app /srv /home/app
USER 10001:10001

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
