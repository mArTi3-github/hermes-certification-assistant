---
name: hermes-agent-notes
description: "Дополнения к hermes-agent: контекст, доступ, шаринг."
metadata:
  hermes:
    tags: [hermes, context, compression, remote-access, profiles, team]
    category: autonomous-ai-agents
    related_skills: [hermes-agent, profile-contribution]
---

# Дополнения к штатному скиллу `hermes-agent`

Базовый скилл `hermes-agent` — **штатный, его нельзя править**: любая правка каталога
отвязывает его от апстрима (`~ hermes-agent (user-modified, skipping)`), и обновления
Hermes перестают приезжать. Всё, чего нет в стоковой версии, живёт здесь.

## Когда применять

Когда вопрос про сам Hermes выходит за рамки стокового `hermes-agent`: размер контекста и
компакция, доступ к бэкенду с другой машины, раздача скиллов команде.

## Что внутри

| Тема | Файл |
|---|---|
| Когда начинать новую сессию, что переживает компакцию, реальный размер сессии на диске | `references/context-and-compression.md` |
| Как дотянуться до бэкенда с другой машины, выбор поверхности (CLI, desktop, gateway) | `references/surfaces-and-remote-desktop.md` |
| Раздача Hermes и скиллов коллегам, дистрибутивы профилей, несколько пользователей на одном хосте | `references/team-and-profile-sharing.md` |

## Правила

1. Сначала загрузить стоковый `hermes-agent` — он описывает установленную версию и обновляется
   вместе с Hermes. Эти заметки его дополняют, а не заменяют.
2. Расхождение с апстримом проверять так: `hermes skills list-modified`,
   `hermes skills diff hermes-agent`. Если стоковая версия начала противоречить фактам на
   стенде — обновить Hermes (`hermes update`), а не править скилл.
3. Появилось что-то, чего нет в стоке → новый файл в `references/` **здесь**, а не в
   `hermes-agent`.
