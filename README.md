# PDF AI Assistant

A command-line tool that lets you **chat with any PDF** or get a **structured summary** of it in any language, powered by the Claude API.

## Features

- **Ask questions about a PDF.** Answers come only from the document and cite page numbers, like *(p. 3)*.
- **One-command summaries** in English, Bulgarian or any other language.
- **Reads the whole PDF**, including tables, charts and scanned pages, using Claude's native PDF support. No manual text extraction.
- **Prompt caching.** The PDF is cached after the first question, so follow-up questions are faster and up to ~90% cheaper.
- **Streaming output.** Answers appear word by word.
- Shows token usage after every answer, so you can see what each request costs.
- Clear error messages for a missing API key, rate limits and network problems.

## Tech stack

Python 3.10+, Anthropic Python SDK, Claude API (PDF documents, prompt caching, streaming)

## Setup

```bash
pip install -r requirements.txt
```

Get an API key at [console.anthropic.com](https://console.anthropic.com) and set it:

```bash
# Windows (PowerShell)
$env:ANTHROPIC_API_KEY = "your-key"

# macOS / Linux
export ANTHROPIC_API_KEY="your-key"
```

## Usage

```bash
# Interactive chat
python pdf_chat.py contract.pdf

# Summary only
python pdf_chat.py report.pdf --summary

# Summary in Bulgarian
python pdf_chat.py report.pdf --summary --lang Bulgarian
```

Example session:

```
Loaded 'annual-report.pdf'. Ask a question (type 'summary' or 'exit').

You: What was the revenue in 2025?

AI: Revenue in 2025 was €4.2 million, a 12% increase over 2024 (p. 7).

[tokens - input: 85, cached: 18240, output: 31]
```

## Configuration

| Variable | Default | Description |
|---|---|---|
| `ANTHROPIC_API_KEY` | – | Your Claude API key (required) |
| `CLAUDE_MODEL` | `claude-opus-5-5` | Model to use. For cheaper requests, set `claude-haiku-5-5` |

## How it works

1. The PDF is base64-encoded and sent as a `document` block with `cache_control`, so it's cached.
2. A system prompt tells the model to answer only from the document and to cite pages.
3. The full conversation history is sent on every turn, so follow-up questions like *"explain that in more detail"* work.
4. If the model declines a request, the request is routed to a fallback model automatically (server-side fallbacks).

Limits: up to 32 MB and 600 pages per PDF.
