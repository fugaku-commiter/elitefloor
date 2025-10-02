from __future__ import annotations

import asyncio
from typing import Any, Dict

from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase
import certifi


class Database:
    def __init__(self, uri: str, db_name: str) -> None:
        # Use certifi CA bundle to avoid SSL handshake issues (e.g., MongoDB Atlas on Windows)
        self._client = AsyncIOMotorClient(uri, tlsCAFile=certifi.where())
        self._db: AsyncIOMotorDatabase = self._client[db_name]

    @property
    def db(self) -> AsyncIOMotorDatabase:
        return self._db

    # Collection helpers
    @property
    def users(self):
        return self._db["users"]  # { _id: userId(str), wallets: [str], completed_tasks: [str] }

    @property
    def zstats(self):
        return self._db["zstats"]  # { _id: wallet(str), total_allocation: float, total_nfts: int, loot_counts: {}, highest_loot: str }

    @property
    def combines(self):
        return self._db["combines"]  # { _id: mainWallet, alts: [walletStr] }

    @property
    def jobs(self):
        return self._db["jobs"]  # snapshot jobs: { _id, type: 'snapshot', status, progress, total }

    @property
    def holders(self):
        return self._db["holders"]  # wallets to snapshot: { _id: walletStr }

    @property
    def hist_rewards(self):
        return self._db["hist_rewards"]  # { _id: epoch('MM/YY'), rewards: { wallet: float } }

    @property
    def verified_wallets(self):
        return self._db["verified_wallets"]  # { _id: wallet(str), user_id: str, verified_at: float }

    @property
    def verifications(self):
        return self._db["verifications"]  # pending verifications: { _id, user_id, wallet, amount_lamports, created_at, status }

    @property
    def evms(self):
        return self._db["EVMS"]  # { _id: user_id(str), evm: str, updated_at: float }

    async def ensure_indexes(self) -> None:
        # _id indexes are implicit/automatic; do not create or set unique on them
        # Create helpful secondary indexes only if needed
        await self.users.create_index("wallets")
        await self.users.create_index("completed_tasks")
        await self.zstats.create_index("total_allocation")
        await self.zstats.create_index("total_nfts")
        await self.holders.create_index("_id")
        await self.hist_rewards.create_index("_id")
        await self.verified_wallets.create_index("_id")
        await self.verified_wallets.create_index("user_id")
        await self.verifications.create_index("user_id")
        await self.evms.create_index("_id")


