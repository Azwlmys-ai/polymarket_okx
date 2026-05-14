#!/bin/bash
# fix_ssl_and_install.sh
# Fixes SSL certificate verification for Homebrew Python venv on macOS
# and installs all project dependencies.
# Usage: bash fix_ssl_and_install.sh

set -e
cd "$(dirname "$0")"

VENV=".venv"
PYTHON="$VENV/bin/python3"
PIP="$VENV/bin/pip"

echo "=== Step 1: Verify venv Python ==="
$PYTHON --version
echo "Interpreter: $($PYTHON -c 'import sys; print(sys.executable)')"

echo ""
echo "=== Step 2: Bootstrap certifi (bypassing SSL for this one install) ==="
$PIP install \
  --trusted-host pypi.org \
  --trusted-host files.pythonhosted.org \
  --trusted-host pypi.python.org \
  --upgrade certifi pip

echo ""
echo "=== Step 3: Get certifi CA bundle path ==="
CERT_FILE=$($PYTHON -c "import certifi; print(certifi.where())")
echo "CA bundle: $CERT_FILE"

echo ""
echo "=== Step 4: Write pip.conf to use certifi permanently ==="
PIP_CONF="$VENV/pip.conf"
cat > "$PIP_CONF" <<EOF
[global]
cert = $CERT_FILE
EOF
echo "Written: $PIP_CONF"
cat "$PIP_CONF"

echo ""
echo "=== Step 5: Install all requirements ==="
$PIP install -r requirements.txt

echo ""
echo "=== Step 6: Verify key packages ==="
$PYTHON -c "import aiohttp; print('aiohttp', aiohttp.__version__)"
$PYTHON -c "import aiosqlite; print('aiosqlite ok')"
$PYTHON -c "import pandas; print('pandas', pandas.__version__)"
$PYTHON -c "import pydantic; print('pydantic', pydantic.__version__)"
$PYTHON -c "import ssl; print('ssl cert file:', ssl.get_default_verify_paths().cafile or 'using certifi')"

echo ""
echo "=== Step 7: Verify SSL works ==="
$PYTHON -c "
import ssl, urllib.request
ctx = ssl.create_default_context()
try:
    urllib.request.urlopen('https://pypi.org', context=ctx, timeout=5)
    print('SSL OK: pypi.org reachable')
except Exception as e:
    print('SSL check:', e)
"

echo ""
echo "============================================================"
echo "✅ All done. Run your project with:"
echo "   .venv/bin/python mvp_runner.py --duration 600"
echo "============================================================"
