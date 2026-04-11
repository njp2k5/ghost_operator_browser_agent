const MAX_MESSAGE_LENGTH = 3200;

const DIVIDER = "──────────";

const LABEL_ICONS = {
    tip: "💡",
    note: "📝",
    warning: "⚠️",
    important: "❗",
    success: "✅",
    error: "🚨",
};

function normalizeText(value) {
    if (value == null) {
        return "";
    }

    return String(value)
        .replace(/\r\n/g, "\n")
        .replace(/\n{3,}/g, "\n\n")
        .trim();
}

function toTitleCase(text) {
    return normalizeText(text)
        .toLowerCase()
        .replace(/\b\w/g, (character) => character.toUpperCase());
}

function cleanInlineText(text) {
    return normalizeText(text).replace(/^[•*\-]\s*/, "");
}

function isBulletLine(line) {
    return /^[•*\-]\s+/.test(line.trim());
}

function isNumberedLine(line) {
    return /^\d+[.)]\s+/.test(line.trim());
}

function stripBullet(line) {
    return line.trim().replace(/^[•*\-]\s+/, "");
}

function stripNumber(line) {
    return line.trim().replace(/^\d+[.)]\s+/, "");
}

function extractLabeledCallout(block) {
    const lines = block.split("\n").map((line) => line.trim()).filter(Boolean);

    if (lines.length === 0) {
        return null;
    }

    const firstLine = lines[0];
    const match = firstLine.match(/^(tip|note|warning|important|success|error)\s*:\s*(.*)$/i);

    if (!match) {
        return null;
    }

    const label = match[1].toLowerCase();
    const firstContent = match[2]?.trim();
    const contentLines = [];

    if (firstContent) {
        contentLines.push(firstContent);
    }

    if (lines.length > 1) {
        contentLines.push(...lines.slice(1));
    }

    return {
        type: "callout",
        label,
        title: toTitleCase(label),
        content: contentLines.join("\n").trim(),
    };
}

function looksLikeHeading(line) {
    const trimmed = line.trim();

    return Boolean(
        trimmed
        && trimmed.endsWith(":")
        && trimmed.length <= 50
        && !isBulletLine(trimmed)
        && !isNumberedLine(trimmed),
    );
}

function parsePlainTextLayout(text) {
    const normalized = normalizeText(text);

    if (!normalized) {
        return {
            title: "",
            subtitle: "",
            blocks: [{ type: "callout", label: "warning", title: "No content", content: "The backend returned an empty reply." }],
        };
    }

    const rawBlocks = normalized.split(/\n\s*\n/).map((block) => block.trim()).filter(Boolean);

    let title = "";
    let subtitle = "";
    const blocks = [];
    let bodySectionCount = 0;

    if (rawBlocks.length > 0) {
        const firstBlockLines = rawBlocks[0].split("\n").map((line) => line.trim()).filter(Boolean);
        if (firstBlockLines.length === 1 && firstBlockLines[0].length <= 60 && !/[.!?]$/.test(firstBlockLines[0])) {
            title = firstBlockLines[0];
            rawBlocks.shift();
        }
    }

    for (const block of rawBlocks) {
        const callout = extractLabeledCallout(block);
        if (callout) {
            blocks.push(callout);
            continue;
        }

        const lines = block.split("\n").map((line) => line.trim()).filter(Boolean);
        if (lines.length === 0) {
            continue;
        }

        if (lines.every(isBulletLine)) {
            blocks.push({
                type: "list",
                title: blocks.length === 0 ? "Highlights" : "Key points",
                items: lines.map(stripBullet),
            });
            continue;
        }

        if (lines.every(isNumberedLine)) {
            blocks.push({
                type: "steps",
                title: "Recommended flow",
                items: lines.map(stripNumber),
            });
            continue;
        }

        if (looksLikeHeading(lines[0])) {
            blocks.push({
                type: "section",
                title: lines[0].replace(/:$/, ""),
                body: lines.slice(1).join("\n"),
            });
            continue;
        }

        if (blocks.length === 0) {
            blocks.push({
                type: "hero",
                body: lines.join("\n"),
            });
            continue;
        }

        bodySectionCount += 1;
        blocks.push({
            type: "section",
            title: bodySectionCount === 1 ? "Details" : `More detail ${bodySectionCount}`,
            body: lines.join("\n"),
        });
    }

    if (blocks.length === 0) {
        blocks.push({ type: "hero", body: normalized });
    }

    return { title, subtitle, blocks };
}

function toStructuredLayout(response) {
    if (typeof response === "string") {
        return parsePlainTextLayout(response);
    }

    if (!response || typeof response !== "object") {
        return parsePlainTextLayout("⚠️ No response");
    }

    if (response.error && !response.reply) {
        return {
            title: response.title || "We hit a snag",
            subtitle: response.subtitle || "There was a problem processing this request",
            blocks: [{
                type: "callout",
                label: "error",
                title: "What happened",
                content: normalizeText(response.error),
            }],
            footer: response.footer || "Please try again in a moment.",
        };
    }

    if (!response.reply && !response.title && !response.sections && !response.summary) {
        return parsePlainTextLayout(JSON.stringify(response, null, 2));
    }

    const blocks = [];
    let parsedReply;

    if (response.summary) {
        blocks.push({ type: "hero", body: normalizeText(response.summary) });
    }

    if (Array.isArray(response.highlights) && response.highlights.length > 0) {
        blocks.push({
            type: "list",
            title: "Highlights",
            items: response.highlights.map(cleanInlineText),
        });
    }

    if (Array.isArray(response.sections)) {
        for (const section of response.sections) {
            if (!section) {
                continue;
            }

            if (Array.isArray(section.items) && section.items.length > 0) {
                blocks.push({
                    type: section.ordered ? "steps" : "list",
                    title: section.title || (section.ordered ? "Steps" : "Section"),
                    items: section.items.map(cleanInlineText),
                });
                continue;
            }

            blocks.push({
                type: "section",
                title: section.title || "Section",
                body: normalizeText(section.body || section.content || ""),
            });
        }
    }

    if (Array.isArray(response.actions) && response.actions.length > 0) {
        blocks.push({
            type: "actions",
            title: response.actionsTitle || "Suggested next moves",
            items: response.actions.map(cleanInlineText),
        });
    }

    if (response.reply) {
        parsedReply = parsePlainTextLayout(response.reply);
        blocks.push(...parsedReply.blocks);
    }

    if (blocks.length === 0) {
        blocks.push({ type: "hero", body: "⚠️ No response" });
    }

    return {
        title: response.title || parsedReply?.title || "",
        subtitle: response.subtitle || parsedReply?.subtitle || "",
        blocks,
        footer: response.footer,
    };
}

function renderHeader(title, subtitle) {
    const normalizedTitle = normalizeText(title);
    const normalizedSubtitle = normalizeText(subtitle);

    if (!normalizedTitle && !normalizedSubtitle) {
        return "";
    }

    const parts = [];

    if (normalizedTitle) {
        parts.push(`✨ *${normalizedTitle}*`);
    }

    if (normalizedSubtitle) {
        parts.push(`_${normalizedSubtitle}_`);
    }

    return parts.join("\n");
}

function renderHero(body) {
    return `🔹 *At a glance*\n${normalizeText(body)}`;
}

function renderSection(title, body) {
    return [`◆ *${normalizeText(title)}*`, normalizeText(body)].filter(Boolean).join("\n");
}

function renderList(title, items) {
    const renderedItems = items
        .map(cleanInlineText)
        .filter(Boolean)
        .map((item) => `• ${item}`)
        .join("\n");

    return [`◆ *${normalizeText(title)}*`, renderedItems].filter(Boolean).join("\n");
}

function renderSteps(title, items) {
    const renderedItems = items
        .map(cleanInlineText)
        .filter(Boolean)
        .map((item, index) => `${index + 1}. ${item}`)
        .join("\n");

    return [`◆ *${normalizeText(title)}*`, renderedItems].filter(Boolean).join("\n");
}

function renderCallout(label, title, content) {
    const icon = LABEL_ICONS[label] || "💬";
    return [`${icon} *${normalizeText(title)}*`, normalizeText(content)].filter(Boolean).join("\n");
}

function renderActions(title, items) {
    const chips = items
        .map(cleanInlineText)
        .filter(Boolean)
        .map((item) => `▸ ${item}`)
        .join("\n");

    return [`🚀 *${normalizeText(title)}*`, chips].filter(Boolean).join("\n");
}

function renderBlock(block) {
    switch (block.type) {
        case "hero":
            return renderHero(block.body);
        case "section":
            return renderSection(block.title, block.body);
        case "list":
            return renderList(block.title, block.items || []);
        case "steps":
            return renderSteps(block.title, block.items || []);
        case "actions":
            return renderActions(block.title, block.items || []);
        case "callout":
            return renderCallout(block.label, block.title, block.content);
        default:
            return normalizeText(block.body || block.content || "");
    }
}

function chunkRenderedSections(sections) {
    const chunks = [];
    let currentChunk = "";

    for (const section of sections.filter(Boolean)) {
        const normalizedSection = normalizeText(section);

        if (!normalizedSection) {
            continue;
        }

        const candidate = currentChunk ? `${currentChunk}\n\n${normalizedSection}` : normalizedSection;
        if (candidate.length <= MAX_MESSAGE_LENGTH) {
            currentChunk = candidate;
            continue;
        }

        if (currentChunk) {
            chunks.push(currentChunk);
        }

        if (normalizedSection.length <= MAX_MESSAGE_LENGTH) {
            currentChunk = normalizedSection;
            continue;
        }

        const lines = normalizedSection.split("\n");
        let longSectionChunk = "";

        for (const line of lines) {
            const candidateLineChunk = longSectionChunk ? `${longSectionChunk}\n${line}` : line;
            if (candidateLineChunk.length <= MAX_MESSAGE_LENGTH) {
                longSectionChunk = candidateLineChunk;
                continue;
            }

            if (longSectionChunk) {
                chunks.push(longSectionChunk);
            }

            longSectionChunk = line;
        }

        currentChunk = longSectionChunk;
    }

    if (currentChunk) {
        chunks.push(currentChunk);
    }

    return chunks;
}

export function formatWhatsAppReply(response) {
    const layout = toStructuredLayout(response);
    const renderedSections = [];
    const header = renderHeader(layout.title, layout.subtitle);

    if (header) {
        renderedSections.push(header, DIVIDER);
    }

    renderedSections.push(...layout.blocks.map(renderBlock));

    if (layout.footer) {
        renderedSections.push(DIVIDER, `🤝 ${normalizeText(layout.footer)}`);
    }

    return chunkRenderedSections(renderedSections);
}