import React, { useState } from 'react'
function Composer({
    connected, busy, blocked, onSend,
}: {
    connected: boolean;
    busy: boolean;
    blocked: boolean; // an approval is pending
    onSend: (text: string) => void;
}) {
    const [input, setInput] = useState('');
    const send = () => {
        if (!input.trim() || busy) return;
        onSend(input);
        setInput('');
    };
    return (
        <div className="flex gap-2.5 border-t border-line-soft py-3.5">
            <input
                value={input}
                onChange={(e) => setInput(e.target.value)}
                onKeyDown={(e) => e.key === 'Enter' && send()}
                placeholder={!connected ? 'Waiting for R.A.V.E.N...' : busy ? 'R.A.V.E.N is working...' : 'Message R.A.V.E.N'}
                disabled={!connected || blocked}
                autoFocus
                className="flex-1 border border-line bg-panel px-3.5 py-2.5 text-[0.92rem] outline-none focus:border-ink disabled:cursor-not-allowed disabled:opacity-45"
            />
            <button
                onClick={send}
                disabled={!connected || busy || blocked || !input.trim()}
                className="cursor-pointer bg-ink px-6 text-[0.85rem] font-semibold text-white disabled:cursor-not-allowed disabled:opacity-45"
            >
                Send
            </button>
        </div>
    );
}

export default Composer