FROM python:3.11-slim

WORKDIR /srv

# 国内构建提速：换 pip 源（不需要可删掉这行）
RUN pip config set global.index-url https://pypi.tuna.tsinghua.edu.cn/simple

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# 下载前端依赖到本地（走 npmmirror 国内镜像；已存在则跳过）
RUN python scripts/download_vendor.py

# HuggingFace 国内镜像，首次启动自动下载 bge 模型时用
ENV HF_ENDPOINT=https://hf-mirror.com

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
