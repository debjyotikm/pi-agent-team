# Validation scope

The automated suite exercises task dispatch, independent review, concurrent actor scheduling with deterministic process doubles, publication conflicts and recovery, message delivery, cancellation obligations, evidence rejection, snapshot execution and context checkpoint behavior. Configuration tests cover roster changes, invalid identities, credential references and context reserves. Export tests cover disallowed runtime files and representative private-data patterns.

The runtime smoke check starts the pinned Pi executable, selects the placeholder model and loads the extension. It does not contact a model endpoint, consume inference credits or evaluate model quality.

CI uses synthetic fixtures and temporary directories. No private task data, conversation records or execution history is required.

A passing local suite establishes behavior on those fixtures. It does not establish backend throughput, arbitrary provider compatibility, large-team capacity, task quality, container execution coverage or the correctness of model-generated work. Before using a configured endpoint for substantial work, run a small disposable task through owner publication, execution and independent review.
