# systemd + checksums bundle

Decode and extract:

```bash
base64 -d archive/2026-09-13/systemd_bundle.b64.txt > /tmp/systemd_bundle.tar.gz
mkdir -p /tmp/cb_systemd && tar -xzf /tmp/systemd_bundle.tar.gz -C /tmp/cb_systemd
sudo cp /tmp/cb_systemd/*.service /tmp/cb_systemd/*.timer /etc/systemd/system/
# SHA256SUMS.txt and MANIFEST_SIZES.txt also inside the tar
```
