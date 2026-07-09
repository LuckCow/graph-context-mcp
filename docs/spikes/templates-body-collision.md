# Spike: create + template + body — which body wins?

Settles the collision flagged in `docs/WORK_PACKAGES.md` under "Templates:
skipped deliberately" — the interaction between a type template's body and
our create-with-`body` path, previously unspiked.

Run against space **GC-E2E**, type **Note**
(`note`), template **(unnamed default template)**.
Reproduce: `python scripts/spike_templates.py`.

## Verdict

**BOTH survive: the template body and the supplied body CONCATENATE (template body first, supplied body appended after).**

- Request field that applies a template on create: `template_id` (the value is the template's object id)
- The same id under the other field names (templateId, template) was **ignored** — no template applied.
- Minting a template via the API (`type_key=template`): HTTP 500 — **not possible**

## What this means for our create-with-body path

Passing a `template_id` does **not** replace or conflict with our `body` — the API stacks the template's body first and appends the supplied `body` after it. So a template can safely default *properties* (e.g. status = To Do) on create, but if the type's template also carries body scaffolding, any `body` we send lands **below** it rather than as the whole page. A template is not a way to override the body; it is additive.

Note: the read-back `markdown` prepends the object's name as its first line (a note-layout quirk) — ignore that line when reading the tables below.

## Bodies observed

| create call | body read back |
| --- | --- |
| template only (`template_id`) | `spike-tplonly-template\_id   \n# Daily note for today   \nThis is your personal journaling practice, so feel free to adj` |
| body only | `spike-bodyonly   \nSPIKE-CUSTOM-BODY the-quick-brown-fox-9f3a   \n` |
| template + body | `spike-template-plus-body   \n# Daily note for today   \nThis is your personal journaling practice, so feel free to adjus` |

Template's own body, for reference:

```
This is your personal journaling practice, so feel free to adjust the prompts or add your own. The most important thing is to take a few moments each day to reflect, set intentions, and appreciate the good things in your life. You can press "Use as a template" button in object settings to modify and save your personalized template   
# ☀️ Morning    
1. *Write a reflection here *   
   
# 🌙 Evening   
1.    

```

## Raw results

```json
{
  "context": {
    "space_name": "GC-E2E",
    "type_key": "note",
    "template_name": "",
    "template_id": "bafyreibe3tfx3ox2pzhgq24v52sym3qpp2zdzduhtbbu33wfqqwepq2e4i"
  },
  "template_creation_probe": {
    "status": 500,
    "created": false,
    "body": "{\"object\":\"error\",\"status\":500,\"code\":\"internal_server_error\",\"message\":\"failed to create object\"}"
  },
  "matrix": {
    "template_field_probe": {
      "template_id": {
        "object_id": "bafyreic7zlzb6cv64mz5oj63puxzpjtexyanhkj37lngfrvwoeyor67r44",
        "markdown": "spike-tplonly-template\\_id   \n# Daily note for today   \nThis is your personal journaling practice, so feel free to adjust the prompts or add your own. The most important thing is to take a few moments each day to reflect, set intentions, and appreciate the good things in your life. You can press \"Use as a template\" button in object settings to modify and save your personalized template   \n# \u2600\ufe0f Morning    \n1. *Write a reflection here *   \n   \n# \ud83c\udf19 Evening   \n1.    \n",
        "template_applied": true
      },
      "templateId": {
        "object_id": "bafyreidxmljmps6m52b5gso2dduzalt2z2pfbyehf3fr7wgfenpfjvq2ym",
        "markdown": "spike-tplonly-templateId   \n",
        "template_applied": false
      },
      "template": {
        "object_id": "bafyreihe72qniqdl3opxxooop55abvb466a6rthk3f6expepmmlaerm4za",
        "markdown": "spike-tplonly-template   \n",
        "template_applied": false
      }
    },
    "template_field_that_works": "template_id",
    "body_only": {
      "object_id": "bafyreietbjixgtqbmfwgslko6x64lwgd7o6zcta3d4bszv4leg635uirny",
      "markdown": "spike-bodyonly   \nSPIKE-CUSTOM-BODY the-quick-brown-fox-9f3a   \n",
      "custom_applied": true
    },
    "template_plus_body": {
      "field": "template_id",
      "object_id": "bafyreifvub4ybrf7uudlyuhbrhvbq3skjni4abs47wbnuej5t6ppwid4he",
      "markdown": "spike-template-plus-body   \n# Daily note for today   \nThis is your personal journaling practice, so feel free to adjust the prompts or add your own. The most important thing is to take a few moments each day to reflect, set intentions, and appreciate the good things in your life. You can press \"Use as a template\" button in object settings to modify and save your personalized template   \n# \u2600\ufe0f Morning    \n1. *Write a reflection here *   \n   \n# \ud83c\udf19 Evening   \n1.    \n   \nSPIKE-CUSTOM-BODY the-quick-brown-fox-9f3a   \n",
      "template_applied": true,
      "custom_applied": true
    }
  }
}
```
