CREATE TABLE IF NOT EXISTS "files" (
    "id" INTEGER PRIMARY KEY AUTOINCREMENT,
    "file_code" TEXT NOT NULL,
    "file_name" TEXT NOT NULL,
    "mime_type" TEXT NOT NULL,
    "storage_key" TEXT NOT NULL,
    "size" INTEGER NOT NULL,
    "order" INTEGER NOT NULL,
    "created_at" DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE UNIQUE INDEX IF NOT EXISTS files_file_code ON files(file_code);
