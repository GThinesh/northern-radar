#!/usr/bin/env python3
"""TLS trust-bundle tests for the IMD downloaders. No network."""
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import certifi

import fetch_kkl as F


def _subjects(pem: str) -> str:
    """`openssl x509` subjects for every cert in a PEM string."""
    lines = [ln.strip() for ln in pem.splitlines()]
    in_cert, certs = False, []
    for ln in lines:
        if ln == "-----BEGIN CERTIFICATE-----":
            in_cert, buf = True, [ln]
        elif in_cert:
            buf.append(ln)
            if ln == "-----END CERTIFICATE-----":
                certs.append("\n".join(buf) + "\n")
                in_cert = False
    out = []
    for c in certs:
        r = subprocess.run(["openssl", "x509", "-noout", "-subject"],
                           input=c, capture_output=True, text=True)
        out.append(r.stdout.strip())
    return "\n".join(out)


class TestCaBundle(unittest.TestCase):
    def test_pinned_roots_present(self):
        # certifi 2026.10.18 dropped the eMudhra emSign roots that
        # mausam.imd.gov.in's chain anchors to. The pinned copy must carry
        # them; that is what fixes "self-signed certificate in chain".
        text = F.IMD_CA.read_text()
        self.assertEqual(text.count("BEGIN CERTIFICATE"), 4)
        self.assertIn("emSign Root CA - G1", _subjects(text))

    def test_bundle_merges_certifi_and_pinned(self):
        bundle = Path(F.ca_bundle()).read_text()
        self.assertIn(F.IMD_CA.read_text().strip(), bundle)
        # certifi's own roots are still carried, so other hosts stay valid.
        certifi_text = Path(certifi.where()).read_text().strip()
        self.assertIn(certifi_text[:200], bundle)


if __name__ == "__main__":
    unittest.main()
