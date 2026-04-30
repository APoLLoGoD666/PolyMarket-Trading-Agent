"""
One-shot script to approve Polymarket contracts for a fresh EOA wallet.
Run from the project root:  python scripts/python/approve_wallet.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from dotenv import load_dotenv
load_dotenv()

from web3 import Web3
from web3.constants import MAX_INT
from web3.middleware import ExtraDataToPOAMiddleware

PRIVATE_KEY = os.getenv("POLYGON_WALLET_PRIVATE_KEY")
if not PRIVATE_KEY:
    print("ERROR: POLYGON_WALLET_PRIVATE_KEY not set in .env")
    sys.exit(1)

RPC = "https://polygon-bor.publicnode.com"
CHAIN_ID = 137

ERC20_ABI = [{"inputs":[{"internalType":"address","name":"spender","type":"address"},{"internalType":"uint256","name":"amount","type":"uint256"}],"name":"approve","outputs":[{"internalType":"bool","name":"","type":"bool"}],"stateMutability":"nonpayable","type":"function"}]
ERC1155_ABI = [{"inputs":[{"internalType":"address","name":"operator","type":"address"},{"internalType":"bool","name":"approved","type":"bool"}],"name":"setApprovalForAll","outputs":[],"stateMutability":"nonpayable","type":"function"}]

USDC_E   = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
USDC     = "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359"
CTF      = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"

CTF_EXCHANGE     = "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"
NEG_RISK_EXCHANGE = "0xC5d563A36AE78145C45a50134d48A1215220f80a"
NEG_RISK_ADAPTER  = "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296"

print(f"Connecting to {RPC}...")
w3 = Web3(Web3.HTTPProvider(RPC, request_kwargs={"timeout": 30}))
w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
if not w3.is_connected():
    print("ERROR: Could not connect to Polygon RPC")
    sys.exit(1)

account = w3.eth.account.from_key(PRIVATE_KEY)
pub_key = account.address
print(f"Wallet: {pub_key}")

usdc_e  = w3.eth.contract(address=Web3.to_checksum_address(USDC_E),  abi=ERC20_ABI)
usdc    = w3.eth.contract(address=Web3.to_checksum_address(USDC),    abi=ERC20_ABI)
ctf     = w3.eth.contract(address=Web3.to_checksum_address(CTF),     abi=ERC1155_ABI)

MAX = int(MAX_INT, 0)

APPROVALS = [
    ("USDC.e approve  → CTF Exchange",      usdc_e.functions.approve(CTF_EXCHANGE, MAX)),
    ("CTF approval    → CTF Exchange",       ctf.functions.setApprovalForAll(CTF_EXCHANGE, True)),
    ("USDC.e approve  → NegRisk Exchange",  usdc_e.functions.approve(NEG_RISK_EXCHANGE, MAX)),
    ("CTF approval    → NegRisk Exchange",  ctf.functions.setApprovalForAll(NEG_RISK_EXCHANGE, True)),
    ("USDC.e approve  → NegRisk Adapter",   usdc_e.functions.approve(NEG_RISK_ADAPTER, MAX)),
    ("CTF approval    → NegRisk Adapter",   ctf.functions.setApprovalForAll(NEG_RISK_ADAPTER, True)),
    ("USDC approve    → CTF Exchange",      usdc.functions.approve(CTF_EXCHANGE, MAX)),
    ("USDC approve    → NegRisk Exchange",  usdc.functions.approve(NEG_RISK_EXCHANGE, MAX)),
    ("USDC approve    → NegRisk Adapter",   usdc.functions.approve(NEG_RISK_ADAPTER, MAX)),
]

for label, fn in APPROVALS:
    print(f"\n[{label}]")
    nonce = w3.eth.get_transaction_count(pub_key)
    try:
        tx = fn.build_transaction({"chainId": CHAIN_ID, "from": pub_key, "nonce": nonce})
        signed = w3.eth.account.sign_transaction(tx, private_key=PRIVATE_KEY)
        tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
        print(f"  Sent:     0x{tx_hash.hex()}")
        receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=600)
        status = "SUCCESS" if receipt.status == 1 else "FAILED"
        print(f"  {status}: block={receipt.blockNumber} gasUsed={receipt.gasUsed}")
    except Exception as e:
        print(f"  ERROR: {e}")

print("\nAll approvals complete.")
