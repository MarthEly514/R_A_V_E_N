import React from 'react'
import { memo, useEffect, useRef, useState } from 'react';
import Markdown from 'react-markdown';
import { Message } from 'src/useRaven';
import remarkGfm from 'remark-gfm';
import rehypeHighlight from 'rehype-highlight';



const MessageBubble = memo(function MessageBubble({ m }: { m: Message }) {
    return (
        <div
            className={[
                'max-w-[78%] px-4 py-3 text-[0.92rem] leading-normal wrap-anywhere',
                m.role === 'user' ? 'self-end bg-ink whitespace-pre-wrap text-white' : 'markdown self-start border border-line bg-panel',
            ].join(' ')}
        >
            {m.role === 'raven' ? (
                // No rehype-raw: raw HTML in a reply stays inert text, never live markup.
                <Markdown remarkPlugins={[remarkGfm]} rehypePlugins={[rehypeHighlight]}>
                    {m.text}
                </Markdown>
            ) : (
                m.text
            )}
        </div>
    );
});

export default MessageBubble
