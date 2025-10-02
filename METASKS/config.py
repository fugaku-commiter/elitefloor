import os
from dataclasses import dataclass, field
from typing import List, Optional

from dotenv import load_dotenv


@dataclass
class Config:
    discord_token: str
    mongo_uri: str
    mongo_db: str = "metasks"
    command_prefix: str = "!"
    magic_eden_base_url: str = "https://api-mainnet.magiceden.dev/v2"
    gen_csv_url: Optional[str] = None  # Optional remote CSV for ranks
    collection_symbol: str = "marketelites"  # Magic Eden collection symbol
    me_api_key: Optional[str] = None  # Magic Eden API key (if required)
    me_request_interval: float = 0.8  # seconds to sleep between ME requests to avoid 429s
    helius_api_key: Optional[str] = None
    collection_key: Optional[str] = None  # On-chain collection address (for Helius getAssetsByGroup)
    # Multiple admin roles supported
    admin_role_ids: List[int] = field(default_factory=lambda: [1420989058288717885, 1422123991119822938])
    # Back-compat single id and alias (first of list)
    admin_role_id: int = 1420989058288717885
    z_id: int = 1420989058288717885
    solana_rpc_url: str = "https://solana-mainnet.g.alchemy.com/v2/5dT-yad_mORfnYWttTcgnfTPHjX1jGhd"


def load_config() -> Config:
    # Load .env from the METASKS folder if present, else project root
    # Allow environment variables to override .env values
    load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), ".env"), override=False)
    load_dotenv(override=False)

    token = os.getenv("token") or os.getenv("DISCORD_TOKEN") or ""
    mongo = os.getenv("mongo") or os.getenv("MONGO_URI") or ""
    if not token:
        raise RuntimeError("DISCORD token is required in .env (key 'token' or 'DISCORD_TOKEN') or environment")
    if not mongo:
        raise RuntimeError("Mongo URI is required in .env (key 'mongo' or 'MONGO_URI') or environment")

    # Admin roles: support 'ADMIN_ROLES' (comma-separated), 'admin_roles', plus single 'admin_role'/'ADMIN_ROLE_ID'
    admin_roles_env = os.getenv("ADMIN_ROLES") or os.getenv("admin_roles") or ""
    admin_role_single = os.getenv("admin_role") or os.getenv("ADMIN_ROLE_ID") or ""
    roles: List[int] = []
    if admin_roles_env:
        for part in admin_roles_env.replace(";", ",").split(","):
            part = part.strip()
            if not part:
                continue
            try:
                roles.append(int(part))
            except Exception:
                pass
    if admin_role_single:
        try:
            roles.append(int(admin_role_single))
        except Exception:
            pass
    if not roles:
        roles = [1420989058288717885, 1422123991119822938]
    # dedupe, preserve order
    seen = set()
    admin_roles_list: List[int] = []
    for rid in roles:
        if rid not in seen:
            admin_roles_list.append(rid)
            seen.add(rid)

    cfg = Config(
        discord_token=token,
        mongo_uri=mongo,
        mongo_db=os.getenv("MONGO_DB", "metasks"),
        command_prefix=os.getenv("COMMAND_PREFIX", "!"),
        magic_eden_base_url=os.getenv("MAGIC_EDEN_BASE_URL", "https://api-mainnet.magiceden.dev/v2"),
        gen_csv_url=os.getenv("GEN_CSV_URL"),
        collection_symbol=os.getenv("ME_COLLECTION", "marketelites"),
        me_api_key=os.getenv("ME_API_KEY"),
        me_request_interval=float(os.getenv("ME_REQUEST_INTERVAL", "0.8")),
        helius_api_key=os.getenv("HELIUS_API_KEY"),
        collection_key=os.getenv("COLLECTION_KEY"),
        admin_role_ids=admin_roles_list,
        admin_role_id=int(admin_roles_list[0]),
        solana_rpc_url=os.getenv("SOLANA_RPC_URL", "https://solana-mainnet.g.alchemy.com/v2/5dT-yad_mORfnYWttTcgnfTPHjX1jGhd"),
    )
    # keep alias in sync
    cfg.z_id = cfg.admin_role_id
    return cfg


