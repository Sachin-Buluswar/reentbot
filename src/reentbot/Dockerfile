FROM --platform=linux/amd64 ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive

# ── Base packages ──
# Ubuntu 24.04 ships GCC 13, Python 3.12, and modern pip/setuptools.
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl wget git ca-certificates build-essential \
    python3 python3-pip python3-venv \
    software-properties-common \
    jq tree \
    && rm -rf /var/lib/apt/lists/*

# ── Node.js (for Hardhat/Truffle projects) ──
RUN curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && npm install -g yarn pnpm \
    && rm -rf /var/lib/apt/lists/*

# ── Solidity: solc-select ──
# The solc download can fail due to GitHub rate limits, so we make it best-effort.
# Forge manages its own solc downloads; only Slither needs system solc.
RUN pip3 install --no-cache-dir --break-system-packages solc-select==1.1.0 \
    && (solc-select install 0.8.28 && solc-select use 0.8.28 || true)

# ── Slither (static analyzer) ──
RUN pip3 install --no-cache-dir --break-system-packages slither-analyzer==0.11.5

# ── Foundry (forge, cast, anvil) ──
RUN curl -L https://foundry.paradigm.xyz | bash \
    && /root/.foundry/bin/foundryup
ENV PATH="/root/.foundry/bin:${PATH}"

# ── Echidna (property-based fuzzer) ──
# Download to file with retries to handle GitHub rate limits gracefully.
RUN ARCH=$(dpkg --print-architecture) && \
    if [ "$ARCH" = "amd64" ]; then \
        ECHIDNA_URL="https://github.com/crytic/echidna/releases/download/v2.3.1/echidna-2.3.1-Linux-x86_64.tar.gz"; \
    elif [ "$ARCH" = "arm64" ]; then \
        ECHIDNA_URL="https://github.com/crytic/echidna/releases/download/v2.3.1/echidna-2.3.1-Linux-aarch64.tar.gz"; \
    fi && \
    (wget --tries=3 --waitretry=10 -O /tmp/echidna.tar.gz "$ECHIDNA_URL" \
        && tar xz -C /usr/local/bin -f /tmp/echidna.tar.gz \
        && rm /tmp/echidna.tar.gz \
        || echo "WARNING: Echidna install failed — agent can install at runtime")

# ── Go (needed for Medusa) ──
RUN ARCH=$(dpkg --print-architecture) && \
    wget --tries=3 --waitretry=10 -O /tmp/go.tar.gz "https://go.dev/dl/go1.24.13.linux-${ARCH}.tar.gz" \
    && tar xz -C /usr/local -f /tmp/go.tar.gz \
    && rm /tmp/go.tar.gz
ENV PATH="/usr/local/go/bin:/root/go/bin:${PATH}"

# ── Medusa (high-throughput fuzzer) ──
RUN go install github.com/crytic/medusa/cmd/medusa@v1.5.0 \
    || echo "WARNING: Medusa install failed — agent can install at runtime"

# ── Halmos (symbolic execution) ──
RUN pip3 install --no-cache-dir --break-system-packages halmos==0.1.13

# ── Workspace setup ──
RUN mkdir -p /audit /workspace /output
# Trust bind-mounted repos (host UID ≠ container root) so forge/git work.
RUN git config --global --add safe.directory '*'
WORKDIR /audit

# Keep container alive for exec
CMD ["sleep", "infinity"]
