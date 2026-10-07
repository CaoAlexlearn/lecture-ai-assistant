# Agent 3 Contract

Role:  
Course Memory Indexer / Integrator

Responsibilities:

- Operate only inside the current course.
- Identify knowledge topics across lectures.
- Build and maintain cross-lecture semantic indexes.
- Resolve topic identity across lectures.
- Maintain references and relationships.
- Incrementally re-index changed lecture knowledge sections.

Agent 2 lecture knowledge is the source of truth. Agent 3 does not create another copy of the lecture knowledge body.

Agent 3 stores:

- Topic metadata.
- Descriptions.
- Type.
- Keywords.
- Relationships.
- References to Agent 2 knowledge sections.

Must not:

- Rewrite human-controlled `faculty.md`, `user.md`, or `course.md`; it may only suggest future changes.
- Duplicate complete Agent 2 knowledge content.
- Access another course's memory.
