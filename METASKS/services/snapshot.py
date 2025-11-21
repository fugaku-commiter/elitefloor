from __future__ import annotations

import asyncio
import time
from typing import Any, Dict, List, Optional

import aiohttp

from ..config import load_config
from ..db import Database


LOOT_HIERARCHY = {
    "Elite": 5,
    "Very High": 4,
    "High": 3,
    "Medium": 2,
    "Low": 1,
}


class SnapshotService:
    def __init__(self, db: Database) -> None:
        self._db = db
        self._cfg = load_config()
        self._session: Optional[aiohttp.ClientSession] = None
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        if self._session is None:
            self._session = aiohttp.ClientSession()

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    async def fetch_nfts_by_wallet(self, wallet_address: str) -> List[Dict[str, Any]]:
        assert self._session is not None
        base = self._cfg.magic_eden_base_url
        url = f"{base}/wallets/{wallet_address}/tokens?offset=0&limit=500"
        headers = {}
        if self._cfg.me_api_key:
            headers["Authorization"] = f"Bearer {self._cfg.me_api_key}"
        attempts = 0
        backoff = max(1.0, float(self._cfg.me_request_interval))
        while attempts < 5:
            try:
                async with self._session.get(url, timeout=30, headers=headers) as resp:
                    if resp.status == 200:
                        nfts = await resp.json()
                        return [n for n in nfts if n.get("collection") == "marketelites"]
                    if resp.status == 429:
                        retry_after = float(resp.headers.get("Retry-After", "0") or "0")
                        sleep_for = retry_after if retry_after > 0 else min(60.0, backoff * 2)
                        await asyncio.sleep(sleep_for)
                        backoff = min(60.0, sleep_for)
                        attempts += 1
                        continue
                    if resp.status in (500, 503):
                        await asyncio.sleep(backoff)
                        attempts += 1
                        backoff = min(60.0, backoff * 2)
                        continue
                    return []
            except Exception as exc:
                print(f"[fetch_nfts_by_wallet] error for {wallet_address}: {exc}; retrying...")
                await asyncio.sleep(backoff)
                attempts += 1
                backoff = min(60.0, backoff * 2)
        return []

    async def fetch_collection_holders(self) -> List[str]:
        """Fetch all unique holder wallet addresses for the configured collection.
        Strategy:
          1) If Helius API key and collection key provided, use Helius getAssetsByGroup to retrieve assets and owners.
          2) Else, fallback to Magic Eden crawl with throttling.
        """
        assert self._session is not None
        holders: set[str] = set()

        # 1) Helius (recommended): groupKey=collection, groupValue=<collection_mint>
        if self._cfg.helius_api_key and self._cfg.collection_key:
            endpoint = f"https://mainnet.helius-rpc.com/?api-key={self._cfg.helius_api_key}"
            page = 1
            page_size = 1000
            print(f"[holders] Helius getAssetsByGroup start collection={self._cfg.collection_key}")
            while True:
                payload = {
                    "jsonrpc": "2.0",
                    "id": "metasks-assets",
                    "method": "getAssetsByGroup",
                    "params": {
                        "groupKey": "collection",
                        "groupValue": self._cfg.collection_key,
                        "page": page,
                        "limit": page_size,
                        "displayOptions": {"showFungible": False},
                    },
                }
                try:
                    async with self._session.post(endpoint, json=payload, timeout=60) as resp:
                        if resp.status != 200:
                            body = await resp.text()
                            print(f"[holders] Helius status={resp.status} body={body[:200]}")
                            break
                        data = await resp.json()
                        items = (data or {}).get("result", {}).get("items", [])
                        print(f"[holders] Helius page={page} items={len(items)}")
                        if not items:
                            break
                        for it in items:
                            # Helius format: current owner is in ownership.owner
                            owner = (
                                (it.get("ownership") or {}).get("owner")
                                or it.get("owner")
                                or (it.get("token_info") or {}).get("owner")
                                or (it.get("authorities") or [{}])[0].get("address")
                            )
                            if owner:
                                holders.add(str(owner))
                        page += 1
                except Exception as exc:
                    print(f"[holders] Helius request error: {exc}; breaking to fallback")
                    break
                await asyncio.sleep(self._cfg.me_request_interval)
            print(f"[holders] Helius holders={len(holders)}")
            if holders:
                return sorted(holders)

        # 2) Fallback to Magic Eden crawling
        base = self._cfg.magic_eden_base_url
        symbol = self._cfg.collection_symbol
        headers = {}
        if self._cfg.me_api_key:
            headers["Authorization"] = f"Bearer {self._cfg.me_api_key}"
        print(f"[holders] ME crawl for collection '{symbol}' base='{base}'")
        # Approach 1: tokens endpoint then owners per token (can be heavy; limit scope)
        # If Magic Eden provides a direct holders endpoint in your environment, swap this for it.
        page = 0
        page_size = 250  # smaller page to reduce 429 likelihood
        backoff = max(0.5, float(self._cfg.me_request_interval))
        while True:
            url = f"{base}/collections/{symbol}/tokens?offset={page*page_size}&limit={page_size}"
            print(f"[holders] GET {url}")
            try:
                async with self._session.get(url, timeout=30, headers=headers) as resp:
                    if resp.status == 429:
                        retry_after = float(resp.headers.get("Retry-After", "0"))
                        sleep_for = retry_after if retry_after > 0 else min(60.0, backoff * 2)
                        backoff = min(60.0, sleep_for)
                        print(f"[holders] tokens 429; sleeping {sleep_for:.1f}s")
                        await asyncio.sleep(sleep_for)
                        continue
                    if resp.status != 200:
                        txt = await resp.text()
                        print(f"[holders] tokens page={page} status={resp.status} body={txt[:200]}")
                        break
                    tokens = await resp.json()
            except Exception as exc:
                print(f"[holders] tokens request error: {exc}; retrying after {backoff:.1f}s")
                await asyncio.sleep(backoff)
                backoff = min(60.0, backoff * 2)
                continue
                print(f"[holders] tokens page={page} count={len(tokens) if isinstance(tokens, list) else 'n/a'}")
                if not tokens or not isinstance(tokens, list):
                    break
                for tok in tokens:
                    mint = tok.get("mintAddress") or tok.get("mint")
                    if not mint:
                        continue
                    # fetch owners for this token
                    own_url = f"{base}/tokens/{mint}/owners"
                try:
                    async with self._session.get(own_url, timeout=30, headers=headers) as own_resp:
                        if own_resp.status == 429:
                            sleep_for = min(60.0, backoff * 2)
                            backoff = sleep_for
                            print(f"[holders] owners 429; sleeping {sleep_for:.1f}s")
                            await asyncio.sleep(sleep_for)
                            continue
                        if own_resp.status == 200:
                            owners = await own_resp.json()
                            # owners is a list; take current owner(s)
                            added = 0
                            if isinstance(owners, list):
                                for o in owners:
                                    w = o.get("owner") or o.get("wallet") or o.get("address")
                                    if w:
                                        holders.add(str(w))
                                        added += 1
                            print(f"[holders] {mint} owners_added={added}")
                        elif own_resp.status in (500, 503):
                            print("[holders] owners 5xx; sleep 3s")
                            await asyncio.sleep(3)
                            continue
                        else:
                            body = await own_resp.text()
                            print(f"[holders] owners status={own_resp.status} body={body[:200]}")
                except Exception as exc:
                    print(f"[holders] owners request error for {mint}: {exc}; continuing")
                    continue
                    # throttle between owner calls
                    await asyncio.sleep(backoff)
                page += 1
                # Optional: safety cap to avoid huge scans; adjust as needed
                if page >= 200:  # up to ~100k tokens scanned
                    break
            # throttle between pages
            await asyncio.sleep(backoff)
        print(f"[holders] unique holders found={len(holders)}")
        return sorted(holders)

    async def process_wallet(self, wallet_address: str) -> Optional[Dict[str, Any]]:
        nfts = await self.fetch_nfts_by_wallet(wallet_address)
        if not nfts:
            return None

        total_percent = 0.0
        total_nfts = 0
        loot_counts: Dict[str, int] = {k: 0 for k in LOOT_HIERARCHY}
        highest_loot: Optional[str] = None

        for nft in nfts:
            if nft.get("collection") != "marketelites":
                continue
            total_nfts += 1
            traits = nft.get("attributes") or nft.get("traits") or []
            for trait in traits:
                trait_type = trait.get("trait_type") or trait.get("traitType")
                value = trait.get("value")
                if not trait_type or value is None:
                    continue
                t = trait_type.lower()
                if t == "percent":
                    try:
                        percent_value = float(str(value).replace("%", "").strip())
                        total_percent += percent_value
                    except ValueError:
                        pass
                elif t == "loot":
                    loot_value_cap = str(value).capitalize()
                    loot_counts[loot_value_cap] = loot_counts.get(loot_value_cap, 0) + 1
                    if (
                        highest_loot is None
                        or LOOT_HIERARCHY.get(loot_value_cap, 0) > LOOT_HIERARCHY.get(highest_loot, 0)
                    ):
                        highest_loot = loot_value_cap

        if total_nfts == 0:
            return None

        total_allocation = (total_percent / 100.0) * 5_983_674
        return {
            "_id": wallet_address,
            "total_allocation": total_allocation,
            "total_nfts": total_nfts,
            "loot_counts": loot_counts,
            "highest_loot": highest_loot,
        }

    async def run_snapshot(self, wallets: List[str], job_id: str, progress_cb=None) -> None:
        await self.start()
        total = len(wallets)
        progress = 0

        # Clear previous snapshot results so stale data isn't shown
        try:
            await self._db.zstats.delete_many({})
        except Exception as exc:  # noqa: BLE001
            # Record the error but continue; snapshot will repopulate
            print(f"[snapshot] Failed to clear zstats: {exc}")

        # Mark job as started
        await self._db.jobs.update_one(
            {"_id": job_id},
            {"$set": {"type": "snapshot", "status": "running", "progress": 0, "total": total, "started_at": time.time()}},
            upsert=True,
        )

        try:
            for wallet in wallets:
                # Check for cancellation
                job_doc = await self._db.jobs.find_one({"_id": job_id})
                if job_doc and job_doc.get("status") == "cancelled":
                    await self._db.jobs.update_one(
                        {"_id": job_id}, {"$set": {"status": "cancelled", "progress": progress}}, upsert=True
                    )
                    return
                doc = await self.process_wallet(wallet)
                if doc:
                    await self._db.zstats.update_one({"_id": doc["_id"]}, {"$set": doc}, upsert=True)
                progress += 1
                await self._db.jobs.update_one(
                    {"_id": job_id}, {"$set": {"progress": progress, "last_wallet": wallet}}, upsert=True
                )
                if progress_cb:
                    await progress_cb(progress, total)
                await asyncio.sleep(1)

            await self._db.jobs.update_one(
                {"_id": job_id}, {"$set": {"status": "completed", "progress": progress, "finished_at": time.time()}}, upsert=True
            )
        except Exception as exc:  # noqa: BLE001
            await self._db.jobs.update_one(
                {"_id": job_id}, {"$set": {"status": "failed", "error": str(exc)}}, upsert=True
            )
            raise

    async def run_snapshot_from_db(self, job_id: str, progress_cb=None) -> None:
        wallets: List[str] = []
        async for doc in self._db.holders.find({}, {"_id": 1}):
            w = str(doc.get("_id", "")).strip()
            if w:
                wallets.append(w)
        if not wallets:
            # Auto-fetch from Magic Eden if holders collection empty
            await self.start()
            wallets = await self.fetch_collection_holders()
            # cache into holders collection
            for w in wallets:
                await self._db.holders.update_one({"_id": w}, {"$set": {"_id": w}}, upsert=True)
        await self.run_snapshot(wallets, job_id, progress_cb=progress_cb)


