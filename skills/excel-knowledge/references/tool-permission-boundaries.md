# Do not simulate tools outside your permissions in code

There is a real case where a readonly sub-agent that has only `execute_python_code_readonly`
(and not `run_script`), wanting to call `excel-read` anyway, wrote code that passed
the argument JSON meant for the `run_script` tool as standard input
via `subprocess`.

```python
subprocess.run(["python", "-m", "json.tool"],
                input='{"skill_name": "excel-read", ...}', ...)
```

This was a mistake of trying to reproduce **the tool call mechanism itself** called `run_script`
in your own code (`json.tool` is a JSON formatting command, not a means of running `excel-read`;
on top of that, passing a string to `input` caused
`TypeError: a bytes-like object is required, not 'str'`).
Tools you do not have permission for cannot be called, however cleverly you write code.

## What to do

- Check the list of tools you have, and as soon as you find that a needed tool (in this example `run_script`)
  is missing, stop trying to work around it in code.
- Solve it through a route that follows the design of tool permissions, such as returning the result to
  a higher-level agent that has the permission, or having the work re-delegated.
- The "readonly" constraint is an intentional design (it lets the agent do read-only investigation safely
  by not giving it write tools), not something to look for loopholes in with code.
