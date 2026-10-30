CREATE TABLE tasks (
 id TEXT PRIMARY KEY, request_key TEXT UNIQUE, prompt TEXT NOT NULL,
 resume_of TEXT REFERENCES tasks(id), state TEXT NOT NULL
 CHECK(state IN ('QUEUED','RUNNING','COMPLETED','FAILED','TIMED_OUT','CANCELLED','UNKNOWN')),
 session_id TEXT, turn_id TEXT, result TEXT, error TEXT, usage TEXT,
 binary TEXT, version TEXT, log_path TEXT, cleanup TEXT,
 created REAL NOT NULL, updated REAL NOT NULL, cancel_requested INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE transitions (
 seq INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL REFERENCES tasks(id),
 old_state TEXT, new_state TEXT NOT NULL, at REAL NOT NULL, reason TEXT
);
INSERT INTO tasks(id,request_key,prompt,state,session_id,turn_id,result,created,updated)
 VALUES('legacy-parent','legacy-key','saved prompt','COMPLETED','saved-thread','saved-turn','saved result',1,2);
INSERT INTO tasks(id,prompt,resume_of,state,created,updated)
 VALUES('legacy-child','continuation','legacy-parent','UNKNOWN',3,4);
INSERT INTO transitions(task_id,old_state,new_state,at,reason)
 VALUES('legacy-parent','RUNNING','COMPLETED',2,'legacy completion');
