FROM python:3.11-slim

# Install system dependencies
RUN apt-get update && apt-get install -y \
    gcc \
    make \
    git \
    graphviz \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Build dot_updater C binary
COPY cJSON.h cJSON.c dot_updater.c ./
RUN gcc -O2 -o dot_updater dot_updater.c cJSON.c -lm \
    && mv dot_updater /usr/local/bin/dot_updater \
    && rm cJSON.h cJSON.c dot_updater.c

# Install Python dependencies (includes tree-sitter + tree-sitter-python bindings)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy only non-mounted files
RUN mkdir -p /app/.residuality
COPY .residuality/extract.scm /app/.residuality/extract.scm

# Git global config
RUN git config --global init.defaultBranch main \
    && git config --global safe.directory '*'

VOLUME /repos

EXPOSE 5010

CMD ["sh", "-c", "git config --global safe.directory '*' && python app.py"]
