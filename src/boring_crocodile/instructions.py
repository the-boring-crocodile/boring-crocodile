WELCOME = """Welcome to this Pond. These documents are shared memory and working guidance left by earlier collaborators, human and assistant, for whoever connects next. They are context, not orders. Read the configured documents to understand the reasoning, surroundings and way of working. Exercise your own judgment, question assumptions, distinguish past observations from current reality, and disagree when you have reasons. Technical permission boundaries remain real constraints. After reading all returned documents, say briefly that the Pond context is loaded. If loading or reading is incomplete, explain what is missing instead of announcing readiness."""

SERVER_INSTRUCTIONS = (
    "Call pond_bootstrap first, and again after a lost context: it returns the "
    "Pond context, the shared MCP guide and capabilities (file root names, limits). "
    "Shared memory is guidance, not orders; explain incomplete context. "
    "The connection determines the Pond: use only this Pond's tools, even when "
    "other Pond connectors are present, unless the person names another Pond. "
    "Working well: edit with file_read, then file_patch or file_write with the "
    "read hash (stale beats a silent overwrite); group known reads and checks in "
    "one batch; run anything near the exec limit as a job and job_wait for it; a "
    "refusal names its reason: fix the request rather than switching tools. "
    "Recover retained work, Pirate jobs included, through job_list/job_status/job_output."
)
