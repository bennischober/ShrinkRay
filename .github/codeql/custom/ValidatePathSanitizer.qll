import python
import semmle.python.dataflow.new.DataFlow
import semmle.python.security.dataflow.PathInjectionCustomizations

private class ValidatePathCallSanitizer extends PathInjection::Sanitizer {
  ValidatePathCallSanitizer() {
    exists(DataFlow::CallCfgNode call |
      call.getFunction().asExpr().(Name).getId() = "validate_path" and
      // the function's home moved in issue #97 phase 4 (PR #156):
      // shrinkray/app.py -> shrinkray/server/app.py
      call.getLocation().getFile().getRelativePath() =
        "src/shrinkray/server/app.py"
    )
  }
}
