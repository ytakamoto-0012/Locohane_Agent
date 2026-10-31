# Help

This assistant is a **local AI agent** that carries out multi-step work on its own,
using prepared "skills" and a set of tools.

---

## 🛠 What it can do

### 1. Run skills
It uses the prepared skills to do specialized work.
> Skills that edit files directly **make a backup automatically** before overwriting and saving.

### 2. Create, edit and read Office documents
**Supported formats**: `.docx` (Word) / `.xlsx`, `.xlsm`, `.xls` (Excel; xls is read-only) / `.pptx` (PowerPoint)

| Feature | Details |
|------|------|
| **Basic operations** | Find and replace, formatting, adding charts and images, etc. |
| **Excel macros (VBA)** | Read, add, overwrite, delete and run code (with security guards) |

### 3. Propose and approve a plan
For multi-step work, a **checklist-style plan is shown before starting**.
1. Review the plan
2. Work starts after you approve it

### 4. Run scripts and code
- It can run scripts included in skills and Python code generated on the spot
- As a rule, a **confirmation message is shown before running** (after a plan is approved, the steps proceed together)
- Long-running work runs **in the background**, so you can keep talking without waiting for it to finish

### 5. Read and present images and files
- It can check and describe the contents of image files
- Files it creates or processes are offered as **download buttons**
- Images can be **previewed** right away

### 6. Persistent memory
It remembers information you want to carry over between conversations and uses it in later conversations.

| Memory type | Contents |
|-----------|------|
| User information | Role, goals, contact people, etc. |
| Feedback | Corrections and confirmed approaches during work |
| Project information | Ongoing work, bugs, issues, etc. |
| References | Pointers to external systems |

> Say "**remember this**" and it is saved automatically.

### 7. Delegate tasks
Independent investigation and research are **delegated to another agent** for efficiency.

### 8. Work with external tools
If configured, additional external tools are available through **MCP servers**.

---

## 💡 Tips

| Point | Description |
|----------|------|
| **Set the working directory** | Use the 📁 **folder icon** in the message box (the default is used if not set) |
| **Starter buttons** | Clicking a button shown at the start of a chat sends its text as is and starts the work |
| **Clarifying questions** | If a request is ambiguous, the assistant may ask you a question first |
| **Run confirmations** | Check the confirmation message shown before running scripts or code, then choose **Allow / Deny** |
| **Display language** | Switch between 日本語 and English from the display settings (palette icon) at the top right |

---

## 📬 Feedback

For feedback, follow the **instructions from your administrator**.
