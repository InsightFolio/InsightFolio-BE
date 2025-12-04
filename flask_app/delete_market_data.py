"""Utility script to purge high-ID rows from the market_data table."""
from __future__ import annotations

import argparse
import os
from pathlib import Path

from dotenv import load_dotenv
from app import create_app, db
from app.models import MarketData


def delete_market_data(
    threshold: int,
    database_url: str | None = None,
    dry_run: bool = False,
    batch_size: int = 1000,
) -> int:
    """
    Delete market_data rows whose ID exceeds the given threshold.

    Returns the number of rows deleted (or 0 for dry runs).
    """
    # Default to DATABASE_URL in flask_app/.env (loaded below) when no override is provided.
    env_path = Path(__file__).resolve().with_name(".env")
    load_dotenv(env_path)
    app = create_app(register_routes=False, database_url=database_url or os.getenv("DATABASE_URL"))
    with app.app_context():
        query = db.session.query(MarketData).filter(MarketData.id > threshold)
        to_delete = query.count()
        if dry_run:
            print(f"{to_delete} rows would be deleted (ID > {threshold}).")
            return 0

        if to_delete == 0:
            print("No rows match the criteria; nothing to delete.")
            return 0

        deleted = 0
        while True:
            # Delete in batches to keep transactions small and reduce undo log usage.
            ids = (
                db.session.query(MarketData.id)
                .filter(MarketData.id > threshold)
                .order_by(MarketData.id)
                .limit(batch_size)
                .all()
            )
            if not ids:
                break
            id_list = [row.id for row in ids]
            deleted += (
                db.session.query(MarketData)
                    .filter(MarketData.id.in_(id_list))
                    .delete(synchronize_session=False)
            )
            db.session.commit()
            print(f"Deleted batch of {len(id_list)} rows (total deleted: {deleted}/{to_delete}).")

        print(f"Finished deleting {deleted} rows from market_data (ID > {threshold}).")
        return deleted


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Delete rows from market_data where ID is above a threshold."
    )
    parser.add_argument(
        "--threshold",
        type=int,
        default=60000,
        help="Delete rows with ID greater than this value (default: 60000).",
    )
    parser.add_argument(
        "--database-url",
        type=str,
        help="Optional SQLAlchemy database URL override (defaults to DATABASE_URL in flask_app/.env).",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=1000,
        help="Number of rows to delete per transaction batch (default: 1000).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Only report how many rows match without deleting.",
    )
    args = parser.parse_args()
    delete_market_data(
        args.threshold,
        database_url=args.database_url,
        dry_run=args.dry_run,
        batch_size=args.batch_size,
    )


if __name__ == "__main__":
    main()
