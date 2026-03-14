FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive

# ── Base packages ──
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl wget git ca-certificates build-essential \
    python3 python3-pip python3-venv \
    software-properties-common \
    && rm -rf /var/lib/apt/lists/*

# ── Solidity: solc-select ──
RUN pip3 install --no-cache-dir solc-select==1.1.0 \
    && solc-select install 0.8.28 \
    && solc-select use 0.8.28

# ── Slither (static analyzer) ──
RUN pip3 install --no-cache-dir slither-analyzer==0.11.5

# ── Foundry (forge, cast, anvil) ──
RUN curl -L https://foundry.paradigm.xyz | bash \
    && /root/.foundry/bin/foundryup
ENV PATH="/root/.foundry/bin:${PATH}"

# ── Echidna (property-based fuzzer) ──
RUN ARCH=$(dpkg --print-architecture) && \
    if [ "$ARCH" = "amd64" ]; then \
        wget -qO- https://github.com/crytic/echidna/releases/download/v2.3.1/echidna-2.3.1-Linux-x86_64.tar.gz | tar xz -C /usr/local/bin; \
    elif [ "$ARCH" = "arm64" ]; then \
        wget -qO- https://github.com/crytic/echidna/releases/download/v2.3.1/echidna-2.3.1-Linux-aarch64.tar.gz | tar xz -C /usr/local/bin; \
    fi

# ── Go (needed for Medusa) ──
RUN ARCH=$(dpkg --print-architecture) && \
    wget -qO- https://go.dev/dl/go1.24.5.linux-${ARCH}.tar.gz | tar xz -C /usr/local
ENV PATH="/usr/local/go/bin:/root/go/bin:${PATH}"

# ── Medusa (high-throughput fuzzer) ──
RUN go install github.com/crytic/medusa/cmd/medusa@v1.5.0

# ── Halmos (symbolic execution) ──
RUN pip3 install --no-cache-dir halmos==0.3.3

# ── Workspace setup ──
RUN mkdir -p /audit /workspace /output
WORKDIR /audit

# Keep container alive for exec
CMD ["sleep", "infinity"]
