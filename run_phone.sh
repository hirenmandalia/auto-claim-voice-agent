#!/bin/bash
# Run the demo for a phone on the same Wi-Fi. Phones need HTTPS for the microphone and camera,
# so this creates a self-signed certificate for this computer's Wi-Fi address on first run.
cd "$(dirname "$0")"
[ -x .venv/bin/python ] || { echo "Run ./setup.sh first."; exit 1; }

IP=$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null || hostname -I 2>/dev/null | awk '{print $1}')
[ -n "$IP" ] || { echo "Could not find this computer's Wi-Fi address. Are you connected?"; exit 1; }

# (Re)create the certificate when it is missing or was made for a different address.
if [ ! -f certs/cert.pem ] || ! openssl x509 -in certs/cert.pem -noout -ext subjectAltName 2>/dev/null | grep -q "IP Address:$IP"; then
  echo "Creating a certificate for $IP ..."
  mkdir -p certs
  openssl req -x509 -newkey rsa:2048 -sha256 -days 365 -nodes \
    -keyout certs/key.pem -out certs/cert.pem \
    -subj "/CN=Claim Voice Demo (local)" \
    -addext "subjectAltName=IP:$IP,IP:127.0.0.1,DNS:localhost" \
    -addext "basicConstraints=CA:TRUE" \
    -addext "keyUsage=digitalSignature,keyEncipherment,keyCertSign" \
    -addext "extendedKeyUsage=serverAuth" 2>/dev/null
  cp certs/cert.pem live_demo/mobile/demo-cert.crt
  NEW_CERT=1
fi

echo
echo "  On your phone (same Wi-Fi):"
if [ -n "$NEW_CERT" ]; then
  echo "  1. First time only, install the certificate:  https://$IP:4443/mobile/demo-cert.crt"
  echo "     iPhone: Settings > Profile Downloaded > Install, then"
  echo "             Settings > General > About > Certificate Trust Settings > turn it on."
  echo "  2. Then open the demo:                        https://$IP:4443/mobile/"
else
  echo "  Open  https://$IP:4443/mobile/"
fi
echo
echo "  Ctrl-C to stop."
echo

export FNOL_ALLOW_LAN=1
exec .venv/bin/python -m uvicorn live_demo.server:app --host 0.0.0.0 --port 4443 \
  --ssl-keyfile certs/key.pem --ssl-certfile certs/cert.pem
