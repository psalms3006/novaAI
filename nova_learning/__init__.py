"""nova_learning — knowledge the person deliberately teaches NOVA.

"Learn this folder" is not "remember this chat" and not "read this PDF":

    inventory    what is in the folder, what can be understood, what is skipped
    extract      principles, preferences, patterns and rules, each with the
                 source it came from (never invented provenance)
    consolidate  merge what many sources agree on, find where they disagree,
                 rank by source authority and recency
    verify       NOVA is tested on the material; only a passed test makes a
                 domain "learned"
    store        per-account, on disk, independent of the model -- survives
                 restarts, new conversations and a change of model
    retrieve     the relevant learned knowledge, for chat, voice and agents
    service      background learning sessions with real progress; resumable;
                 incremental re-learning when the folder changes

Kept apart from personal memory (facts about the person), project context
and capability knowledge (nova_skills), as the brief requires.

Vocabulary is strict: read (accessed), analyzed (processed), learned
(structured knowledge created *and verified*), remembered (persisted to
personal memory). Nothing here says "learned" before verification passes.
"""
