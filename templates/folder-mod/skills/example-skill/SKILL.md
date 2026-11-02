---
name: example-mod-skill
description: Example skill bundled with a Hermes folder mod. Automatically discovered when the mod is enabled.
---

# Example Mod Skill

This skill is bundled directly inside a Hermes folder mod (`<mod-dir>/skills/<skill-name>/SKILL.md`).

## Discovery & Precedence

1. When the mod has `enabled: true`, its `skills/` directory is automatically discovered by Hermes Agent.
2. The skill appears in `skills_list`, `skill_view`, and the agent's system prompt `available_skills`.
3. If the mod is disabled (`enabled: false`), the skill is automatically unloaded from runtime discovery.
4. User profile skills (`~/.hermes/skills/`) have precedence over mod-bundled skills if names collide.

## Instructions

- Use this template to provide specialized instructions, domain workflows, and helper scripts for your mod.
- Supporting files (references, templates, scripts) can be placed alongside `SKILL.md`.
