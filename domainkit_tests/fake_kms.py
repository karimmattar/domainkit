"""Key services in memory that speak their wire protocols: AWS KMS and Vault transit.

``FakeKms`` answers KMS's JSON API (``GenerateDataKey``, ``Decrypt``,
``ReEncrypt``) and checks each request's SigV4 signature itself, from the
request as sent, against the secret of the access key it names: a request
whose signed headers or body changed on the way is refused, as KMS would.
Its keys rotate (``rotate``): ciphertexts name the version that made them,
and every version still decrypts.

``FakeVault`` answers the transit engine's ``datakey/plaintext``,
``decrypt`` and ``rewrap``, with ``vault:v<N>:`` ciphertexts, versions, and
``min_decryption_version``, and checks ``X-Vault-Token``.

Both are handlers for ``domainkit.http.client.stub``.
"""

import base64
import hashlib
import hmac
import json
import secrets
from dataclasses import dataclass, field
from typing import Any, Final
from urllib.parse import quote

import httpx2
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

SECRETS: Final = {"AKIDEXAMPLE": "wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY"}
"""Access keys the fake KMS knows, and their secrets."""


def _error(status: int, kind: str) -> httpx2.Response:
    """KMS's error answer: JSON naming the exception."""
    return httpx2.Response(
        status, json={"__type": f"com.amazonaws.kms#{kind}", "message": "refused"}
    )


@dataclass
class FakeKms:
    """KMS keys in memory, by id and alias; see the module."""

    keys: dict[str, list[bytes]] = field(default_factory=dict)
    aliases: dict[str, str] = field(default_factory=dict)
    calls: list[str] = field(default_factory=list)
    """The operations asked, in order."""
    bodies: list[bytes] = field(default_factory=list)

    def create(self, key_id: str, alias: str | None = None) -> str:
        """A new key; its id."""
        self.keys[key_id] = [AESGCM.generate_key(bit_length=256)]
        if alias is not None:
            self.aliases[alias] = key_id
        return key_id

    def rotate(self, key_id: str) -> None:
        """Give ``key_id`` a new version: new ciphertexts use it."""
        self.keys[key_id].append(AESGCM.generate_key(bit_length=256))

    def version_of(self, blob: str) -> tuple[str, int]:
        """The key and version a base64 ciphertext was made with."""
        data = base64.b64decode(blob)
        return data[2 : 2 + data[1]].decode(), data[0]

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        """Answer one call of KMS's JSON API."""
        if not self._signed(request):
            return _error(403, "InvalidSignatureException")
        operation = request.headers["x-amz-target"].removeprefix("TrentService.")
        self.calls.append(operation)
        self.bodies.append(request.content)
        body = json.loads(request.content)
        try:
            handler = getattr(self, f"_{operation}")
        except AttributeError:
            return _error(400, "UnknownOperationException")
        return handler(body)  # type: ignore[no-any-return]

    def _resolve(self, key_id: str) -> str | None:
        """A key's id from its id, ARN, or alias."""
        key_id = self.aliases.get(key_id, key_id).rpartition("/")[2]
        return key_id if key_id in self.keys else None

    def _seal(self, key_id: str, plaintext: bytes, context: dict[str, str]) -> str:
        """``plaintext`` under the newest version of ``key_id``, as base64."""
        version = len(self.keys[key_id]) - 1
        nonce = secrets.token_bytes(12)
        aad = json.dumps(context, sort_keys=True).encode()
        sealed = AESGCM(self.keys[key_id][version]).encrypt(nonce, plaintext, aad)
        name = key_id.encode()
        return base64.b64encode(
            bytes([version, len(name)]) + name + nonce + sealed
        ).decode()

    def _open(self, blob: str, context: dict[str, str]) -> tuple[str, bytes] | None:
        """The key id and plaintext of a ciphertext; None if it does not open."""
        try:
            data = base64.b64decode(blob, validate=True)
            version, size = data[0], data[1]
            key_id = data[2 : 2 + size].decode()
            rest = data[2 + size :]
            key = self.keys[key_id][version]
            aad = json.dumps(context, sort_keys=True).encode()
            return key_id, AESGCM(key).decrypt(rest[:12], rest[12:], aad)
        except (ValueError, IndexError, KeyError, InvalidTag):
            return None

    def _GenerateDataKey(self, body: dict[str, Any]) -> httpx2.Response:
        key_id = self._resolve(body["KeyId"])
        if key_id is None:
            return _error(400, "NotFoundException")
        assert body["KeySpec"] == "AES_256"
        plaintext = secrets.token_bytes(32)
        return httpx2.Response(
            200,
            json={
                "KeyId": key_id,
                "Plaintext": base64.b64encode(plaintext).decode(),
                "CiphertextBlob": self._seal(
                    key_id, plaintext, body.get("EncryptionContext", {})
                ),
            },
        )

    def _Decrypt(self, body: dict[str, Any]) -> httpx2.Response:
        opened = self._open(body["CiphertextBlob"], body.get("EncryptionContext", {}))
        if opened is None:
            return _error(400, "InvalidCiphertextException")
        if self._resolve(body["KeyId"]) != opened[0]:
            return _error(400, "IncorrectKeyException")
        return httpx2.Response(
            200,
            json={
                "KeyId": opened[0],
                "Plaintext": base64.b64encode(opened[1]).decode(),
            },
        )

    def _ReEncrypt(self, body: dict[str, Any]) -> httpx2.Response:
        opened = self._open(
            body["CiphertextBlob"], body.get("SourceEncryptionContext", {})
        )
        if opened is None or self._resolve(body["SourceKeyId"]) != opened[0]:
            return _error(400, "InvalidCiphertextException")
        destination = self._resolve(body["DestinationKeyId"])
        assert destination is not None
        blob = self._seal(
            destination, opened[1], body.get("DestinationEncryptionContext", {})
        )
        return httpx2.Response(200, json={"CiphertextBlob": blob, "KeyId": destination})

    @staticmethod
    def _signed(request: httpx2.Request) -> bool:
        """Whether SigV4 signs ``request`` as sent, recomputed here from scratch."""
        authorization = request.headers.get("authorization", "")
        try:
            scheme, rest = authorization.split(" ", 1)
            parts = dict(item.strip().split("=", 1) for item in rest.split(","))
            access_key, day, region, service, terminal = parts["Credential"].split("/")
        except ValueError:
            return False
        secret = SECRETS.get(access_key)
        if scheme != "AWS4-HMAC-SHA256" or secret is None or terminal != "aws4_request":
            return False
        names = parts["SignedHeaders"].split(";")
        if not {"host", "x-amz-date"} <= set(names):
            return False
        canonical = "\n".join(
            [
                request.method,
                quote(request.url.path or "/", safe="/-_.~"),
                "",
                "".join(f"{name}:{request.headers[name].strip()}\n" for name in names),
                parts["SignedHeaders"],
                hashlib.sha256(request.content).hexdigest(),
            ]
        )
        stamp = request.headers["x-amz-date"]
        scope = f"{day}/{region}/{service}/aws4_request"
        to_sign = "\n".join(
            [
                "AWS4-HMAC-SHA256",
                stamp,
                scope,
                hashlib.sha256(canonical.encode()).hexdigest(),
            ]
        )
        key = f"AWS4{secret}".encode()
        for piece in (day, region, service, "aws4_request"):
            key = hmac.new(key, piece.encode(), hashlib.sha256).digest()
        expected = hmac.new(key, to_sign.encode(), hashlib.sha256).hexdigest()
        return stamp.startswith(day) and hmac.compare_digest(
            expected, parts["Signature"]
        )


@dataclass
class FakeVault:
    """One Vault transit engine in memory; see the module."""

    token: str = "s.test-token"
    mount: str = "transit"
    keys: dict[str, list[bytes]] = field(default_factory=dict)
    min_decryption: dict[str, int] = field(default_factory=dict)
    seen: list[httpx2.Request] = field(default_factory=list)

    def create(self, name: str) -> None:
        """A new transit key, at version 1."""
        self.keys[name] = [AESGCM.generate_key(bit_length=256)]
        self.min_decryption[name] = 1

    def rotate(self, name: str) -> None:
        """Give ``name`` a new version."""
        self.keys[name].append(AESGCM.generate_key(bit_length=256))

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        """Answer one call of the transit engine."""
        self.seen.append(request)
        if request.headers.get("x-vault-token") != self.token:
            return httpx2.Response(403, json={"errors": ["permission denied"]})
        path = request.url.path.removeprefix(f"/v1/{self.mount}/")
        action, _, name = path.rpartition("/")
        if name not in self.keys:
            return httpx2.Response(400, json={"errors": ["encryption key not found"]})
        body = json.loads(request.content or b"{}")
        if action == "datakey/plaintext":
            plaintext = secrets.token_bytes(body.get("bits", 256) // 8)
            return self._data(
                plaintext=base64.b64encode(plaintext).decode(),
                ciphertext=self._seal(name, plaintext),
            )
        opened = self._open(name, str(body.get("ciphertext", "")))
        if opened is None:
            return httpx2.Response(400, json={"errors": ["invalid ciphertext"]})
        if action == "decrypt":
            return self._data(plaintext=base64.b64encode(opened).decode())
        if action == "rewrap":
            return self._data(ciphertext=self._seal(name, opened))
        return httpx2.Response(404, json={"errors": []})

    @staticmethod
    def _data(**data: str) -> httpx2.Response:
        """Vault's answer: the data under ``data``."""
        return httpx2.Response(200, json={"request_id": "x", "data": data})

    def _seal(self, name: str, plaintext: bytes) -> str:
        """``vault:v<N>:`` and the plaintext under the newest version."""
        version = len(self.keys[name])
        nonce = secrets.token_bytes(12)
        sealed = AESGCM(self.keys[name][version - 1]).encrypt(nonce, plaintext, None)
        return f"vault:v{version}:{base64.b64encode(nonce + sealed).decode()}"

    def _open(self, name: str, ciphertext: str) -> bytes | None:
        """The plaintext; None if it does not open, or its version is too old."""
        try:
            prefix, version_text, payload = ciphertext.split(":", 2)
            version = int(version_text.removeprefix("v"))
            if prefix != "vault" or version < self.min_decryption[name]:
                return None
            data = base64.b64decode(payload, validate=True)
            key = self.keys[name][version - 1]
            return AESGCM(key).decrypt(data[:12], data[12:], None)
        except (ValueError, IndexError, InvalidTag):
            return None
