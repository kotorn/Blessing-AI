import asyncio
import os
import asyncpg

async def main():
    host = os.environ.get("POSTGRES_HOST")
    port = int(os.environ.get("POSTGRES_PORT", "5432"))
    database = os.environ.get("POSTGRES_DB", "blessing_trading")
    user = os.environ.get("POSTGRES_USER", "blessing_worker")
    password = os.environ.get("POSTGRES_PASSWORD")

    print(f"Connecting to Postgres at {host}:{port}/{database} as {user}...")
    conn = await asyncpg.connect(
        host=host,
        port=port,
        database=database,
        user=user,
        password=password,
    )
    print("Connected.")

    rows_before = await conn.fetch("SELECT launch_id, approval_id, symbol, policy, state, submitted_orders FROM mainnet_launch_sessions")
    print(f"Sessions before update ({len(rows_before)}):")
    for r in rows_before:
        print(" ", dict(r))

    res = await conn.execute(
        """
        UPDATE mainnet_launch_sessions
        SET state = 'CLOSED', updated_at = CURRENT_TIMESTAMP
        WHERE symbol = 'ETHUSDC'
          AND submitted_orders = 0
          AND state != 'CLOSED'
        """
    )
    print(f"Update result: {res}")

    rows_after = await conn.fetch("SELECT launch_id, approval_id, symbol, policy, state, submitted_orders FROM mainnet_launch_sessions")
    print(f"Sessions after update ({len(rows_after)}):")
    for r in rows_after:
        print(" ", dict(r))

    await conn.close()
    print("Done.")

if __name__ == "__main__":
    asyncio.run(main())
