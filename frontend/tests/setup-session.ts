import { beforeEach } from "vitest"

import { resetAccessSessionModule } from "../src/api/sessionTokens"

beforeEach(() => {
  resetAccessSessionModule()
})
