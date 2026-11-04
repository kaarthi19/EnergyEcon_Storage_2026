# Shared helpers for the command-line scripts.
include(joinpath(@__DIR__, "..", "src", "BESSBidding.jl"))
using .BESSBidding, DataFrames, Dates

"Parse `--key value` pairs (and bare `--flag`) into a Dict."
function parse_args(args)
    d = Dict{String,String}()
    i = 1
    while i <= length(args)
        a = args[i]
        startswith(a, "--") || error("unexpected argument $a")
        k = a[3:end]
        if i < length(args) && !startswith(args[i+1], "--")
            d[k] = args[i+1]; i += 2
        else
            d[k] = "true"; i += 1
        end
    end
    return d
end

const HACKATHON_DIR = normpath(joinpath(@__DIR__, ".."))
default_config() = joinpath(HACKATHON_DIR, "configs", "default.toml")

"Row index of the first timestamp >= `s` (accepts a timestamp string or a row number)."
function row_of(df, s::AbstractString)
    all(isdigit, s) && return parse(Int, s)
    t = DateTime(s)
    i = findfirst(>=(t), df.timestamp)
    i === nothing && error("timestamp $s is after the end of the data")
    return i
end
