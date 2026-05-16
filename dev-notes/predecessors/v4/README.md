# pi-python-harness

A Python-first prototype for using Pi as an agentic harness while keeping custom tools and orchestration in Python.

The library starts Pi in `--mode rpc`, manages its JSONL stdin/stdout protocol, starts a local Python tool broker, and generates a small generic TypeScript extension that registers Python-authored tools with Pi.

This is an initial implementation. It was tested against a fake Pi RPC process and the Python tool broker in this environment. The actual Pi package could not be run here because the container has Node 18, while the uploaded Pi repository and its packages require Node 20+, and npm dependencies were not cached.
