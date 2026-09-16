FROM node@sha256:83f487e0a63425e5b4d146fb5e5be574bcbe1b7b843d3ebafdd95eaf7767a7e5
RUN apt-get update && apt-get install -y --no-install-recommends python3 python3-venv git ripgrep ca-certificates \
    && rm -rf /var/lib/apt/lists/*
ENV HOME=/tmp PYTHONDONTWRITEBYTECODE=1
WORKDIR /workspace
USER 1000:1000
ENTRYPOINT ["sleep", "infinity"]
