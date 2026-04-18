# Full pipeline setup
Binaries (must be on PATH)
Tool	Used by	Install
terraform	terraform_validate, terraform_plan	hashicorp.com/terraform
checkov	checkov_runner	pip install checkov
conftest	opa_runner	conftest.dev
claude	nl_quality (LLM judge)	Claude Code CLI — already installed
docker	LocalStack	already installed
Services

# LocalStack (terraform_plan needs it on :4566)
docker rm -f localstack
docker run -d -p 4566:4566 \
  -e LOCALSTACK_AUTH_TOKEN=ls-fuXaVoDi-1652-KEWo-kaMA-8556nOwu85aa \
  --name localstack localstack/localstack
Terraform workspace init (one-time)

cd tf_workspace
terraform init -backend=false
Environment variables (API keys)

export GROQ_API_KEY=...
export GOOGLE_API_KEY=...
export MISTRAL_API_KEY=...
# ANTHROPIC_API_KEY — needed by `claude -p` for the nl_quality judge
export ANTHROPIC_API_KEY=...
Python deps (stdlib + requests only)

pip install requests
OPA policies
The opa_runner looks for .rego files in policies/ — make sure that directory has your policies (they're currently tracked in git under policies/).

Two things to fix in your existing setup file:

The docker run command is missing \ after the port flag
terraform init _backend=false should be terraform init -backend=false (dash, not underscore)