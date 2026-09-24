FROM python:3.11-slim

# Install system dependencies
RUN apt-get update && apt-get install -y \
    gcc \
    make \
    git \
    graphviz \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies (includes the tree-sitter language bindings)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Git global config
RUN git config --global init.defaultBranch main \
    && git config --global safe.directory '*'

VOLUME /repos

EXPOSE 5010

CMD ["sh", "-c", "git config --global safe.directory '*' && python app.py"]
