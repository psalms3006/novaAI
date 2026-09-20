You are now taking responsibility for understanding and advancing the NOVA repository.

Do NOT begin by immediately editing files.

Your first responsibility is to completely understand what this repository currently is, what it is supposed to become, what already works, what is broken, what is incomplete, and what prevents it from being a genuinely reliable NOVA product.

Treat this as a serious engineering takeover and repository audit.

Read the repository broadly before making architectural decisions.

# CONTEXT: WHAT I AM TRYING TO BUILD

NOVA is a voice-first AI operating system assistant under OMNIEL.

The goal is not to build another chatbot.

NOVA should become an intelligent computer assistant capable of understanding natural voice requests and actually operating the user's computer.

The broader product vision includes:

* Voice-first interaction
* Continuous/open-microphone interaction where appropriate
* Natural conversational interaction
* Speech recognition
* Speech synthesis
* Multimodal understanding
* Screen/vision awareness
* Application launching
* File management
* Browser control
* Web search
* Computer/system settings control
* Planning
* Multi-step task execution
* Persistent memory
* Knowledge retrieval
* Model/provider agnosticism
* Intelligent model routing
* Network-aware behavior
* Graceful online/offline fallback
* Local AI capability
* Local knowledge access
* Tool execution
* Self-improvement/self-editing capabilities where safely implemented
* A polished desktop application
* A distributable executable
* An installer
* A ZIP/distribution package
* Eventually mobile/PWA experiences

The user experience should feel like an AI operating layer over the computer, not merely a text interface.

The current project has historically involved technologies and concepts including:

* Faster-Whisper
* Gemini / Gemini Live
* Groq
* Ollama
* Local models such as Mistral
* Piper TTS
* pyttsx3 fallback
* FAISS
* SentenceTransformers
* Kiwix/ZIM knowledge
* Model routing
* Computer-control tools
* Browser control
* File control
* System settings
* Vision
* Planning
* Persistent memory
* Desktop packaging

Do NOT assume that every technology above is still correctly implemented.

Verify the actual repository.

The repository itself is the source of truth for the current implementation.

# PHASE 1: REPOSITORY RECONNAISSANCE

Start with a complete structural inspection.

Determine:

* Repository root
* Directory structure
* Important files
* Entry points
* Application startup flow
* Frontend
* Backend
* Services
* Modules
* Configuration
* Environment files
* Dependency manifests
* Build scripts
* Packaging scripts
* Tests
* Assets
* Models
* Databases/vector stores
* Knowledge files
* Installer configuration
* EXE/build configuration
* Distribution artifacts
* Documentation
* Git state

Search broadly.

Do not rely only on filenames.

Trace imports and execution paths.

Identify which files are actually used at runtime versus legacy/dead code.

# PHASE 2: UNDERSTAND THE RUNTIME

Trace NOVA from startup to user interaction.

I want you to understand:

1. How NOVA starts
2. What process starts first
3. How the backend starts
4. How the frontend/UI starts
5. How the voice pipeline starts
6. How microphone input is handled
7. How speech is converted into text/audio understanding
8. How the request reaches the intelligence layer
9. How the model/provider is selected
10. How tools are selected
11. How tool execution works
12. How responses are generated
13. How responses reach TTS
14. How audio reaches the user
15. How memory is stored
16. How memory is retrieved
17. How failures are handled
18. How fallbacks work
19. How shutdown works

Draw the actual runtime architecture mentally and explain it in your audit.

# PHASE 3: AI / INFERENCE AUDIT

Audit the intelligence layer in detail.

Determine:

* Which models/providers actually exist
* Which providers are actually usable
* Which are configured
* Which are dead or partially integrated
* How API keys/configuration are loaded
* How requests are routed
* Whether model capabilities are represented
* Whether model fallback exists
* Whether network failure is handled
* Whether timeout handling exists
* Whether provider failure is handled
* Whether context survives fallback
* Whether tool calling works
* Whether structured tool execution works
* Whether the model can distinguish tools from normal responses
* Whether the architecture is genuinely provider-agnostic or only appears to be

Identify hard-coded provider assumptions.

# PHASE 4: VOICE PIPELINE AUDIT

Audit the entire voice system.

Determine:

* STT implementation
* TTS implementation
* Audio input handling
* Audio output handling
* Microphone lifecycle
* Continuous listening behavior
* Voice activity detection
* Wake-word behavior if implemented
* Interruption handling
* Latency
* Error handling
* Offline behavior
* Fallback behavior
* Audio device assumptions
* Threading/async behavior
* Resource consumption

Test the pipeline where possible.

Do not merely inspect code and conclude that voice works.

If physical microphone interaction is required, perform everything else you can and clearly identify the exact physical test that remains.

# PHASE 5: TOOL / COMPUTER CONTROL AUDIT

Audit every tool/module capable of interacting with the computer.

Determine whether each is:

* Implemented
* Imported
* Registered
* Callable by the agent
* Actually executed
* Properly validated
* Properly error-handled
* Properly logged
* Actually working

Audit things such as:

* open_app
* web_search
* file_controller
* computer_settings
* browser_control
* vision
* planner
* self_editor
* Any other tools discovered in the repository

Do not assume the module name means the functionality works.

Trace each tool through the complete path:

model → tool selection → tool dispatch → execution → result → model → user.

# PHASE 6: MEMORY AUDIT

Audit the memory architecture.

Determine:

* What is stored
* Where it is stored
* How embeddings are generated
* How retrieval works
* How conversation context is handled
* Whether persistent memory actually persists
* Whether memory is automatically injected
* Whether irrelevant memories can pollute responses
* Whether failures are handled
* Whether the memory subsystem is actually connected to the main runtime

Verify FAISS/SentenceTransformer or whatever the repository actually uses.

# PHASE 7: KNOWLEDGE / OFFLINE AUDIT

Audit local knowledge functionality.

Determine:

* Whether Kiwix/ZIM is integrated
* How knowledge is indexed/accessed
* Whether retrieval actually works
* Whether it is available offline
* What happens when knowledge files are missing
* What happens when the network disappears
* Whether local knowledge can be used as a fallback

Do not assume the presence of ZIM files means the feature works.

# PHASE 8: UI / DESKTOP APPLICATION AUDIT

Audit the actual desktop interface.

Determine:

* Frontend technology
* Startup mechanism
* Backend/frontend communication
* UI state management
* Voice state visualization
* Ambient orb/waveform implementation
* Conversation interface
* Error states
* Loading states
* Settings
* Configuration
* Responsiveness
* Broken UI flows
* Development-only assumptions

Determine whether the UI actually connects to the real NOVA backend.

Do not evaluate only screenshots or source appearance.

Run it where possible.

# PHASE 9: PACKAGING AUDIT

This is extremely important.

NOVA is intended to be distributed.

Audit:

* EXE
* Installer
* ZIP
* Build scripts
* Runtime paths
* Bundled resources
* Python/runtime dependencies
* Environment variables
* API configuration
* Assets
* Models
* Knowledge files
* Frontend assets
* Backend startup
* Desktop shortcut behavior
* Installation behavior

Determine whether the packaged application is actually capable of running independently of the development environment.

If an EXE or installer exists, test the actual artifact where possible.

Do not assume:

"build succeeded = application works."

# PHASE 10: DEPENDENCY AND ENVIRONMENT AUDIT

Inspect:

* requirements files
* package manifests
* lock files
* Python version assumptions
* Node version assumptions
* build tooling
* runtime dependencies
* OS dependencies
* environment variables
* optional dependencies
* platform-specific assumptions

Look for:

* Missing dependencies
* Unused dependencies
* Conflicting versions
* Broken imports
* Development-only dependencies
* Absolute paths
* User-specific paths
* Hard-coded machine assumptions

# PHASE 11: PERFORMANCE AUDIT

Look for obvious or measurable bottlenecks.

Pay attention to:

* CPU consumption
* RAM consumption
* Startup time
* Model loading
* Duplicate model loading
* Multiple processes
* Thread leaks
* Async issues
* Memory leaks
* Excessive polling
* Unnecessary network requests
* Large startup workloads

Do not optimize blindly.

Identify actual bottlenecks where possible.

# PHASE 12: SECURITY AUDIT

Check for:

* Exposed secrets
* Hard-coded API keys
* Unsafe command execution
* Arbitrary file operations
* Unsafe shell execution
* Unvalidated tool arguments
* Browser-control risks
* Self-editing risks
* Privilege escalation risks
* Unsafe network interfaces
* Sensitive information in logs

Do not expose any discovered secret in your output.

If you find a serious security issue, flag it prominently.

# PHASE 13: TEST AUDIT

Determine:

* What tests currently exist
* What they test
* Whether they actually run
* Whether they are meaningful
* What critical functionality has no tests
* What integration tests exist
* What runtime tests exist
* What packaging tests exist

Run the existing test suite.

Do not assume passing tests mean the product works.

Identify the most important missing tests.

# PHASE 14: GIT / REPOSITORY HEALTH

Inspect:

* Current branch
* Working tree
* Recent commits
* Uncommitted changes
* Ignored files
* Large files
* Build artifacts
* Potential secrets
* Repository size
* Potential Git problems

Do not push anything.

Do not destroy existing user work.

# PHASE 15: KNOWN HISTORY

There have previously been problems around:

* NOVA launching without a visible window
* Flask backend startup
* CLOSE_WAIT / FIN_WAIT connections
* "NoneType object is not callable"
* Ollama/model availability
* API-key fallback behavior
* Offline fallback
* Packaging
* EXE behavior
* Installer behavior
* Large repository objects
* ZIM files
* Embedder/model files
* Disk-space issues
* Dependency installation
* Runtime/environment differences

These are historical clues, NOT assumptions that the current repository still has these bugs.

Investigate whether any remain.

# PHASE 16: PRODUCE A COMPLETE AUDIT

Before making major modifications, produce a structured audit containing:

## Executive Diagnosis

Explain in plain language:

* What NOVA currently is
* What it already does well
* What is genuinely working
* What is partially working
* What is broken
* What is missing
* What is architecturally weak
* What is preventing production readiness

## Actual Architecture

Describe the real architecture discovered in the repository.

Include the flow of:

User → Voice/UI → Intelligence → Tools → Memory → Response → TTS/UI

where applicable.

## Capability Matrix

Create a table containing major NOVA capabilities with statuses such as:

* Working
* Working but fragile
* Partially implemented
* Broken
* Not implemented
* Unknown / requires physical testing

## Critical Bugs

Rank bugs by:

* P0: blocks core product operation
* P1: major functionality broken
* P2: important but non-blocking
* P3: polish/minor

## Architecture Problems

Identify structural problems that will make NOVA harder to scale, maintain, or distribute.

## Security Problems

Rank security issues by severity.

## Packaging Problems

Explain exactly what prevents reliable distribution.

## Testing Gaps

Explain what currently has insufficient verification.

## Recommended Execution Plan

Create a prioritized plan for bringing NOVA to a reliable state.

The plan should distinguish:

1. Immediate blockers
2. Reliability fixes
3. Architecture fixes
4. Feature completion
5. Packaging
6. UX polish
7. Future improvements

# IMPORTANT: DO NOT STOP AT STATIC ANALYSIS

After producing the audit, begin addressing the problems yourself.

Do not wait for me to manually approve every ordinary engineering fix.

You have autonomy to investigate, implement, test, rebuild, and iterate.

However, before making a major product-direction or high-impact architectural decision that materially changes NOVA's intended behavior, explain the decision and ask me.

For ordinary engineering decisions, proceed.

# THE WORK LOOP

For each important problem:

1. Reproduce it.
2. Diagnose the root cause.
3. Fix it.
4. Run the relevant test.
5. Test the failure path.
6. Run regression tests.
7. Continue to the next issue.

Do not stop after fixing only the first visible error.

If fixing one issue exposes another, continue.

If a test fails, investigate and fix it.

If a build fails, investigate and fix it.

If packaging fails, investigate and fix it.

If runtime behavior differs from development behavior, investigate and fix it.

# DEFINITION OF DONE

Do NOT consider NOVA finished simply because the repository builds.

Continue until all realistically testable critical functionality has been verified.

For each major subsystem, establish evidence for its state.

At minimum, investigate and verify:

* Application startup
* UI/backend connection
* AI inference
* Model/provider selection
* Tool calling
* Core tools
* Voice pipeline
* Memory
* Offline/fallback behavior
* Error handling
* Configuration
* Packaging
* EXE
* Installer
* ZIP/distribution artifacts

When something requires physical interaction, tell me exactly what I need to do and why.

Continue with all other work that can be automated.

# ARTIFACT SYNCHRONIZATION

Whenever your changes affect the distributable application:

* Update source
* Rebuild affected artifacts
* Verify the build
* Verify the actual artifact
* Keep EXE, installer, ZIP and source synchronized

Do not leave stale distributables behind.

# FINAL VERIFICATION

Before claiming completion, perform a final verification pass.

Ask yourself:

"Do I have evidence that this works?"

If the answer is no, keep investigating/testing.

Do not say "should work."

Do not say "looks good."

Do not fabricate evidence.

Do not claim something passed unless it actually passed.

At the end, provide:

1. Final implementation summary
2. Bugs fixed
3. Tests actually executed
4. Results of those tests
5. Packaging verification
6. Remaining issues
7. Physical tests I need to perform
8. Git commits created
9. Any risks or technical debt remaining

You are not here merely to modify files.

You are here to help turn the repository into a reliable, working NOVA product.

Start with reconnaissance.

Do not make major changes until you understand the repository.

For substantial tasks, do not immediately implement. First determine whether the task benefits from multi-agent investigation. When appropriate, assemble a small specialist team. Specialists should independently investigate their assigned dimension and provide evidence, assumptions, risks, and recommendations. Do not treat majority agreement as proof. Resolve disagreements using evidence from the codebase, tests, documentation, and reproducible observations. The main agent acts as the final technical decision-maker.

For implementation tasks, establish acceptance criteria before coding. After implementation, independently verify the result through appropriate tests, integration checks, regression checks, security review, and end-to-end validation. If verification fails, return the issue to investigation rather than declaring the task complete. Never claim a task works merely because the code looks correct.
