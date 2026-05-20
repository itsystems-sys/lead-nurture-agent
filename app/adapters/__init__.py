"""CRM adapters.

Each CRM (Close, HubSpot, Salesforce, ...) ships as a single ``adapters/<name>.py``
module that implements the ``CrmAdapter`` protocol defined in
``adapters.base``. Adapters translate CRM-specific webhook payloads into
``NormalizedWebhookEvent`` values that the engine's state machine knows how to
act on. The core (state machine, scheduler, workflows, templates) never sees
CRM-specific structure.

How to add a new CRM
--------------------

1. Create ``app/adapters/<crm>.py``. Define a class implementing
   :class:`app.adapters.base.CrmAdapter` and instantiate a module-level
   ``adapter = MyCrmAdapter()`` singleton.
2. Add the CRM's config (API key, custom field IDs, status IDs) to
   :class:`app.config.Settings` with a ``CRM_`` env-var prefix.
3. Add a single route entry in :mod:`app.routes.api` that imports the new
   adapter and uses :func:`app.routes.api._handle_crm_webhook` to dispatch.
4. Document any CRM quirks (Salesforce platform events vs. Outbound Messages,
   HubSpot's app token model, etc.) in the adapter module's docstring.

The engine's internal model (LeadStatus, OpportunityStatus, the transition
state machine, scheduler, workflow JSONs, templates) does not change when a
new CRM is added.
"""

from app.adapters.base import CrmAdapter, NormalizedWebhookEvent

__all__ = ["CrmAdapter", "NormalizedWebhookEvent"]
