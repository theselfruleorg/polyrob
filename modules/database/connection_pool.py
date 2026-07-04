"""Database connection pooling for SQLite with proper resource management."""

import asyncio
import sqlite3
import logging
from typing import Optional, List, Dict, Any
from contextlib import asynccontextmanager
from pathlib import Path
import time
from queue import Queue, Empty
import threading

from core.exceptions import DatabaseError


class ConnectionPool:
    """SQLite connection pool with proper resource management."""

    def __init__(
        self,
        db_path: str,
        min_connections: int = 2,
        max_connections: int = 10,
        timeout: float = 30.0,
        logger: Optional[logging.Logger] = None
    ):
        """Initialize connection pool.

        Args:
            db_path: Path to SQLite database file
            min_connections: Minimum number of connections to maintain
            max_connections: Maximum number of connections allowed
            timeout: Connection timeout in seconds
            logger: Optional logger instance
        """
        self.db_path = db_path
        self.min_connections = min_connections
        self.max_connections = max_connections
        self.timeout = timeout
        self.logger = logger or logging.getLogger(__name__)

        # Connection pool
        self._pool = Queue(maxsize=max_connections)
        self._connections_created = 0
        self._lock = threading.Lock()
        self._closed = False

        # Statistics
        self._stats = {
            'connections_created': 0,
            'connections_reused': 0,
            'connections_closed': 0,
            'wait_time_total': 0,
            'active_connections': 0
        }

        # Initialize minimum connections
        self._initialize_pool()

    def _initialize_pool(self):
        """Initialize the minimum number of connections."""
        for _ in range(self.min_connections):
            try:
                conn = self._create_connection()
                self._pool.put(conn)
            except Exception as e:
                self.logger.error(f"Failed to create initial connection: {e}")

    def _create_connection(self) -> sqlite3.Connection:
        """Create a new database connection."""
        with self._lock:
            if self._connections_created >= self.max_connections:
                raise DatabaseError(f"Maximum connections ({self.max_connections}) reached")

            conn = sqlite3.connect(
                self.db_path,
                check_same_thread=False,
                timeout=self.timeout
            )
            conn.row_factory = sqlite3.Row

            # Enable foreign keys and optimize for concurrent access
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("PRAGMA journal_mode = WAL")  # Write-Ahead Logging for better concurrency
            conn.execute("PRAGMA synchronous = NORMAL")  # Balance between safety and speed
            conn.execute("PRAGMA cache_size = -64000")  # 64MB cache
            conn.execute("PRAGMA temp_store = MEMORY")  # Use memory for temp tables

            self._connections_created += 1
            self._stats['connections_created'] += 1

            self.logger.debug(f"Created new connection (total: {self._connections_created})")
            return conn

    def get_connection(self, timeout: Optional[float] = None) -> sqlite3.Connection:
        """Get a connection from the pool.

        Args:
            timeout: Optional timeout override

        Returns:
            Database connection

        Raises:
            DatabaseError: If unable to get connection
        """
        if self._closed:
            raise DatabaseError("Connection pool is closed")

        timeout = timeout or self.timeout
        start_time = time.time()

        try:
            # Try to get existing connection
            try:
                conn = self._pool.get(block=False)
                self._stats['connections_reused'] += 1
                self._stats['active_connections'] += 1

                # Test if connection is still valid
                try:
                    conn.execute("SELECT 1")
                    return conn
                except:
                    # Connection is dead, create new one
                    self.logger.warning("Dead connection detected, creating new one")
                    self._connections_created -= 1
                    return self._create_connection()

            except Empty:
                # No connections available, create new one if under limit
                with self._lock:
                    if self._connections_created < self.max_connections:
                        conn = self._create_connection()
                        self._stats['active_connections'] += 1
                        return conn

                # Wait for connection to become available
                self.logger.debug(f"Waiting for connection (timeout: {timeout}s)")
                conn = self._pool.get(block=True, timeout=timeout)

                # Test connection validity
                try:
                    conn.execute("SELECT 1")
                    self._stats['connections_reused'] += 1
                    self._stats['active_connections'] += 1
                    return conn
                except:
                    # Connection is dead, create new one
                    self._connections_created -= 1
                    conn = self._create_connection()
                    self._stats['active_connections'] += 1
                    return conn

        except Empty:
            wait_time = time.time() - start_time
            self._stats['wait_time_total'] += wait_time
            raise DatabaseError(f"Connection pool exhausted (waited {wait_time:.2f}s)")
        except Exception as e:
            self.logger.error(f"Error getting connection: {e}")
            raise DatabaseError(f"Failed to get connection: {e}")
        finally:
            wait_time = time.time() - start_time
            self._stats['wait_time_total'] += wait_time

    def return_connection(self, conn: sqlite3.Connection):
        """Return a connection to the pool.

        Args:
            conn: Connection to return
        """
        if self._closed:
            conn.close()
            return

        try:
            # Reset connection state
            conn.rollback()  # Ensure no pending transactions

            self._stats['active_connections'] -= 1

            # Return to pool if healthy
            try:
                conn.execute("SELECT 1")
                self._pool.put(conn, block=False)
            except:
                # Connection is dead, close it
                self.logger.warning("Closing dead connection")
                conn.close()
                with self._lock:
                    self._connections_created -= 1
                    self._stats['connections_closed'] += 1

        except Exception as e:
            self.logger.error(f"Error returning connection: {e}")
            try:
                conn.close()
            except:
                pass
            with self._lock:
                self._connections_created -= 1
                self._stats['connections_closed'] += 1

    @asynccontextmanager
    async def get_connection_async(self):
        """Async context manager for getting a connection."""
        conn = None
        try:
            # Get connection in thread pool to avoid blocking
            loop = asyncio.get_event_loop()
            conn = await loop.run_in_executor(None, self.get_connection)
            yield conn
        finally:
            if conn:
                await loop.run_in_executor(None, self.return_connection, conn)

    def close(self):
        """Close all connections in the pool."""
        self._closed = True

        # Close all connections
        while not self._pool.empty():
            try:
                conn = self._pool.get_nowait()
                conn.close()
                self._stats['connections_closed'] += 1
            except:
                pass

        self._connections_created = 0
        self.logger.info(f"Connection pool closed. Stats: {self._stats}")

    def get_stats(self) -> Dict[str, Any]:
        """Get pool statistics."""
        return {
            **self._stats,
            'pool_size': self._pool.qsize(),
            'total_connections': self._connections_created,
            'available_connections': self._pool.qsize()
        }

    def __del__(self):
        """Cleanup on deletion."""
        if not self._closed:
            self.close()


class PooledDatabaseConnection:
    """Database connection wrapper that uses connection pooling."""

    def __init__(self, db_path: str, logger: Optional[logging.Logger] = None):
        """Initialize pooled database connection.

        Args:
            db_path: Path to SQLite database file
            logger: Optional logger instance
        """
        self.db_path = db_path
        self.logger = logger or logging.getLogger(__name__)

        # Create connection pool
        self.pool = ConnectionPool(
            db_path=db_path,
            min_connections=2,
            max_connections=20,  # Increased for production load
            timeout=30.0,
            logger=self.logger
        )

        self._lock = asyncio.Lock()

    async def execute(self, query: str, params: tuple = ()) -> Any:
        """Execute a query using a pooled connection."""
        async with self.pool.get_connection_async() as conn:
            cursor = conn.execute(query, params)
            conn.commit()
            return cursor

    async def fetch_one(self, query: str, params: tuple = ()) -> Optional[Dict[str, Any]]:
        """Fetch one row using a pooled connection."""
        async with self.pool.get_connection_async() as conn:
            cursor = conn.execute(query, params)
            row = cursor.fetchone()
            if row:
                columns = [description[0] for description in cursor.description]
                return dict(zip(columns, row))
            return None

    async def fetch_all(self, query: str, params: tuple = ()) -> List[Dict[str, Any]]:
        """Fetch all rows using a pooled connection."""
        async with self.pool.get_connection_async() as conn:
            cursor = conn.execute(query, params)
            rows = cursor.fetchall()
            if rows:
                columns = [description[0] for description in cursor.description]
                return [dict(zip(columns, row)) for row in rows]
            return []

    async def close(self):
        """Close the connection pool."""
        self.pool.close()

    def get_stats(self) -> Dict[str, Any]:
        """Get pool statistics."""
        return self.pool.get_stats()