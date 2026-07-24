# Commit-signing preflight

Use this before the orchestrator creates the first commit when repository-local or global Git
configuration enables signing.

1. Inspect signing configuration without printing key material:

   ```bash
   git config --show-origin --get commit.gpgsign
   git config --show-origin --get gpg.format
   git config --show-origin --get gpg.ssh.program
   ```

2. If signing delegates to an external agent, such as a password-manager SSH signer, verify its
   session with a non-secret status command. Do not print account identifiers, tokens, private
   keys, or secret values. A configured account is not necessarily an active session.
3. Keep the staged diff intact and retry the commit only after the configured signer is available.
   If signing continues to fail, stop and ask the user to unlock or sign in to the configured
   agent. Do not use `--no-gpg-sign`, replace the signing key, or change global Git settings to
   bypass the gate.
4. After a successful commit, verify that it exists and satisfies the repository's signing policy
   before pushing. Record only the commit SHA and verification status.
