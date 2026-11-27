# Stage F-R v3 cell budget semantics

The existing Stage F dispatcher resolves the v3 experiment. A verified cell admission rejection becomes a measured failure with effective completion and delivered success both false. The independently observed Oracle verdict stays visible. Actual resource violations, unknown usage, missing rejection proof, provider failures and infrastructure failures stop the matrix. The existing per-cell cost, batch and global stops still apply.

The guard records headroom and estimate under the admission lock. Final audit reconciles settled usage, request counts, actual prompt and completion tokens, and remaining capacity against the request journal. It checks that the terminal rejection preceded the next provider request. A request exceeding its estimate is recorded separately from a cell exceeding its nominal cap. The plan, manifest and predecessor batch checks bind the stop-policy digest.

The v2 formal evidence remains historical. Its recorded TOKEN_STOP_REACHED result is unchanged.

## Contract review

The formal v3 freeze is on hold. C1's hidden Oracle requires a specific production timeout that is absent from the public interface and seed profile table. The Owner must resolve the scoring contract before using the formal preparation entry point. Default-runner admission can be inspected without creating a manifest, authorizations or claims.

After the scoring contract is resolved and the execution checkout is reviewed, the existing entry point prepares a fresh external evidence root with the scoped v3 plans. Launch requires three signed authorizations for that exact freeze.

~~~powershell
& ./.venv/Scripts/python.exe -m experiments.stage_f.release_candidate prepare --out D:/Work/fr3-release
& ./.venv/Scripts/python.exe -m experiments.stage_f.release_candidate launch --out D:/Work/fr3-release
~~~
