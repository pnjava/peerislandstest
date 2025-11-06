# Codebase Analyzer (LLM + Static Analysis)

This tool analyzes a target repository (for example, [SakilaProject](https://github.com/janjakovacevic/SakilaProject)) and produces a structured JSON report that captures:

- A high-level overview of the project
- Noteworthy frameworks, libraries, and cross-cutting concerns
- Key classes and methods with signatures, concise descriptions, and cyclomatic complexity scores

## How it works

1. Scans relevant source files (`.java`, `.kt`, `.py`, `.cs`, `.js`, `.ts`).
2. Parses Java sources with [`javalang`](https://github.com/c2nes/javalang) and collects class/method metadata.
3. Computes cyclomatic complexity with [`lizard`](https://github.com/terryyin/lizard).
4. Uses LangChain to orchestrate map/reduce prompts against your preferred LLM provider (OpenAI, Azure OpenAI, or Amazon Bedrock).
5. Emits a strict JSON artifact summarizing the repository.

## Quick start

```bash
# Clone the target repository (example: SakilaProject)
git clone https://github.com/janjakovacevic/SakilaProject

# Create and activate a virtual environment
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt

# Configure your preferred LLM provider credentials
export OPENAI_API_KEY=...        # or
export AZURE_OPENAI_API_KEY=...  # or
export AWS_ACCESS_KEY_ID=...
export AWS_SECRET_ACCESS_KEY=...

# Run the analyzer
python main.py --repo ../SakilaProject --provider openai --model gpt-4o-mini --out output/sakila.json
```

## Output

The generated JSON (`output/sakila.json` in the example) contains:

- `repo`: absolute path to the analyzed repository
- `generated_at`: UTC timestamp
- `tech_stack`: parsed Maven dependencies and highlighted frameworks (Spring Boot, Thymeleaf, MySQL, etc.)
- `high_level_overview`, `noteworthy_aspects`, `cross_cutting_concerns`, `possible_gaps`: aggregated project insights
- `packages`: nested breakdown of packages → classes → methods, including signatures, line counts, complexity, summaries, risks, and external calls

## Extending

- Add more file extensions to `SUPPORTED_EXTENSIONS` in `main.py` to handle additional languages.
- Extend the parser section to capture metadata from non-Java files (currently only Java structures are parsed for method summaries).
- Customize the prompts in `prompts.py` to request additional metadata or adjust the output schema.

## License

This project is provided as-is for the PeerIslands technical exercise.
